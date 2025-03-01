from __future__ import annotations

import contextlib
import datetime
import json
import os
import warnings
from dataclasses import asdict
from pathlib import PosixPath

import datatree as dt
import fsspec
import pandas as pd
import rechunker
import xarray as xr
import zarr
from carbonplan_data.metadata import get_cf_global_attrs
from carbonplan_data.utils import set_zarr_encoding as set_web_zarr_encoding
from ndpyramid import pyramid_regrid
from prefect import task
from prefect.triggers import any_failed
from upath import UPath
from xarray_schema import DataArraySchema, DatasetSchema
from xarray_schema.base import SchemaError

from ... import __version__ as version, config
from ...data.cmip import get_gcm
from ...data.observations import open_era5
from ...utils import str_to_hash
from .containers import RunParameters, TimePeriod
from .utils import (
    blocking_to_zarr,
    calc_auspicious_chunks_dict,
    is_cached,
    resample_wrapper,
    set_zarr_encoding,
    subset_dataset,
    validate_zarr_store,
)

xr.set_options(keep_attrs=True)
warnings.filterwarnings(
    "ignore",
    "(.*) filesystem path not explicitly implemented. falling back to default implementation. This filesystem may not be tested",
    category=UserWarning,
)

PIXELS_PER_TILE = 128
scratch_dir = UPath(config.get("storage.scratch.uri"))
intermediate_dir = UPath(config.get("storage.intermediate.uri")) / version
results_dir = UPath(config.get("storage.results.uri")) / version
use_cache = config.get('run_options.use_cache')


@task(log_stdout=True)
def make_run_parameters(**kwargs) -> RunParameters:
    """Prefect task wrapper for RunParameters"""
    return RunParameters(**kwargs)


@task(log_stdout=True)
def get_obs(run_parameters: RunParameters) -> UPath:
    """Task to return observation data subset from input parameters.

    Parameters
    ----------
    run_parameters : RunParameters
        RunParameter dataclass defined in common/conatiners.py. Constructed from prefect parameters.

    Returns
    -------
    UPath
        Path to subset observation dataset.
    """
    feature_string = '_'.join(run_parameters.features)
    frmt_str = "{obs}_{feature_string}_{latmin}_{latmax}_{lonmin}_{lonmax}_{train_dates[0]}_{train_dates[1]}".format(
        **asdict(run_parameters), feature_string=feature_string
    )
    title = f"obs ds: {frmt_str}"
    ds_hash = str_to_hash(frmt_str)
    target = intermediate_dir / 'get_obs' / ds_hash

    if use_cache and is_cached(target):
        print(f'found existing target: {target}')
        return target
    print(run_parameters)
    ds = open_era5(run_parameters.features, run_parameters.train_period)
    subset = subset_dataset(
        ds,
        run_parameters.features,
        run_parameters.train_period.time_slice,
        run_parameters.bbox,
        chunking_schema={'time': 365, 'lat': 150, 'lon': 150},
    )

    for key in subset.variables:
        subset[key].encoding = {}

    subset.attrs.update({'title': title}, **get_cf_global_attrs(version=version))
    subset = set_zarr_encoding(subset)
    blocking_to_zarr(ds=subset, target=target, validate=True, write_empty_chunks=True)

    return target


@task(log_stdout=True)
def get_experiment(run_parameters: RunParameters, time_subset: str) -> UPath:
    """Prefect task that returns cmip GCM data from input run parameters.

    Parameters
    ----------
    run_parameters : RunParameters
        RunParameter dataclass defined in common/conatiners.py. Constructed from prefect parameters.
    time_subset : str
        String describing time subset request. Either 'train_period', 'predict_period', or 'both'.

    Returns
    -------
    UPath
        UPath to experiment dataset.
    """

    if time_subset == 'both':
        time_period = TimePeriod(
            start=str(
                min(
                    int(run_parameters.train_period.start), int(run_parameters.predict_period.start)
                )
            ),
            stop=str(
                max(int(run_parameters.train_period.stop), int(run_parameters.predict_period.stop))
            ),
        )
    else:
        time_period = getattr(run_parameters, time_subset)

    features = getattr(run_parameters, 'features')
    if features:
        feature_string = '_'.join(features)
        frmt_str = "{model}_{member}_{scenario}_{feature_string}_{latmin}_{latmax}_{lonmin}_{lonmax}_{time_period.start}_{time_period.stop}".format(
            time_period=time_period, **asdict(run_parameters), feature_string=feature_string
        )

    else:
        frmt_str = "{model}_{member}_{scenario}_{variable}_{latmin}_{latmax}_{lonmin}_{lonmax}_{time_period.start}_{time_period.stop}".format(
            time_period=time_period, **asdict(run_parameters)
        )

    if int(time_period.start) < 2015 and run_parameters.scenario != 'historical':
        scenarios = ['historical', run_parameters.scenario]
    else:
        scenarios = [run_parameters.scenario]

    title = f"experiment ds: {frmt_str}"
    ds_hash = str_to_hash(frmt_str)
    target = intermediate_dir / 'get_experiment' / ds_hash

    if use_cache and is_cached(target):
        print(f'found existing target: {target}')
        return target

    mode = 'w'
    for feature in run_parameters.features:
        ds_list = []
        for s in scenarios:
            ds_list.append(
                get_gcm(
                    scenario=s,
                    member_id=run_parameters.member,
                    table_id=run_parameters.table_id,
                    grid_label=run_parameters.grid_label,
                    source_id=run_parameters.model,
                    variable=feature,
                    time_slice=time_period.time_slice,
                )
            )
        ds = xr.concat(ds_list, dim='time')
        subset = subset_dataset(ds, feature, time_period.time_slice, run_parameters.bbox)
        # Note: dataset is chunked into time:365 chunks to standardize leap-year chunking.
        subset = subset.chunk({'time': 365})
        for key in subset.variables:
            subset[key].encoding = {}
        subset.attrs.update({'title': title}, **get_cf_global_attrs(version=version))
        subset = set_zarr_encoding(subset)
        subset[[feature]].to_zarr(target, mode=mode)
        mode = 'a'

    return target


@task(log_stdout=True)
def rechunk(
    path: UPath,
    pattern: str = None,
    template: UPath = None,
    max_mem: str = "5GB",
) -> UPath:
    """Use `rechunker` package to adjust chunks of dataset to a form
    conducive for your processing.

    Parameters
    ----------
    path : UPath
        path to zarr store
    pattern : str
        The pattern of chunking you want to use. If used together with `template` it will override the template
        to ensure that the final dataset truly follows that `full_space` or `full_time` spec. This matters when you are passing
        a template that is either a shorter time length or a template that is a coarser grid (and thus a shorter lat/lon chunksize)
    template : UPath
        The path to the file you want to use as a chunking template. The utility will grab the chunk sizes and use them as the chunk
        target to feed to rechunker.
    max_mem : str
        The memory available for rechunking steps. Must look like "2GB". Optional, default is 5GB.

    Returns
    -------
    target : UPath
        Path to rechunked dataset
    """
    # if both defined then you'll take the spatial part of template and override one dimension with the specified pattern
    if template is not None:
        pattern_string = 'matched'
        if pattern is not None:
            pattern_string += f'_{pattern}'
    elif pattern is not None:
        pattern_string = pattern

    task_hash = str_to_hash(str(path) + pattern_string + str(template) + max_mem)
    target = intermediate_dir / 'rechunk' / task_hash
    path_tmp = scratch_dir / 'rechunk' / task_hash

    target_store = fsspec.get_mapper(str(target))
    temp_store = fsspec.get_mapper(str(path_tmp))

    if use_cache and is_cached(target):
        print(f'found existing target: {target}')
        # if we wanted to check that it was chunked correctly we could put this down below where
        # the target_schema is validated. but that requires us going through the development
        # of the schema would just hurt performance likely unnecessarily.
        # nevertheless, as future note: if we encounter chunk issues i suggest putting a schema check here
        return target
    # if a cached target isn't found we'll go through the rechunking step
    # open the zarr group
    target_store.clear()
    temp_store.clear()
    group = zarr.open_consolidated(path)
    # open the dataset to access the coordinates
    ds = xr.open_zarr(path)
    example_var = list(ds.data_vars)[0]
    # if you have defined a template then use the chunks of that template
    # to form the desired chunk definition
    if template is not None:
        template_ds = xr.open_zarr(template)
        # define the chunk definition
        chunk_def = {
            'time': min(template_ds.chunks['time'][0], len(ds.time)),
            'lat': min(template_ds.chunks['lat'][0], len(ds.lat)),
            'lon': min(template_ds.chunks['lon'][0], len(ds.lon)),
        }
        # if you have also defined a pattern then override the dimension you've specified there
        if pattern is not None:
            # the chunking pattern will return the dimensions that you'll chunk along
            # so `full_time` will return `('lat', 'lon')`
            chunk_dims = config.get(f"chunk_dims.{pattern}")
            for dim in chunk_def:
                if dim not in chunk_dims:
                    # override the chunksize of those unchunked dimensions to be the complete length (like passing chunksize=-1
                    chunk_def[dim] = len(ds[dim])
    elif pattern is not None:
        chunk_dims = config.get(f"chunk_dims.{pattern}")
        chunk_def = calc_auspicious_chunks_dict(ds[example_var], chunk_dims=chunk_dims)
    else:
        raise AttributeError('must either define chunking pattern or template')
    # Note:
    # for rechunker v 0.3.3:
    # initialize the chunks_dict that you'll pass in, filling the coordinates with
    # `None`` because you don't want to rechunk the coordinate arrays. this works with
    # for rechunker v 0.4.2:
    # initialize chunks_dict using the `chunk_def`` above
    chunks_dict = {
        'time': (chunk_def['time'],),
        'lon': (chunk_def['lon'],),
        'lat': (chunk_def['lat'],),
    }
    for var in ds.data_vars:
        chunks_dict[var] = chunk_def
    # now that you have your chunks_dict you can check that the dataset at `path`
    # you're passing in doesn't already match that schema. because if so, we don't
    # need to bother with rechunking and we'll skip it!
    schema_dict = {var: DataArraySchema(chunks=chunk_def) for var in ds.data_vars}
    target_schema = DatasetSchema(schema_dict)
    with contextlib.suppress(SchemaError):
        # check to see if the initial dataset already matches the schema, in which case just
        # return the initial path and work with that
        target_schema.validate(ds)
        return path
    rechunk_plan = rechunker.rechunk(
        source=group,
        target_chunks=chunks_dict,
        max_mem=max_mem,
        target_store=target_store,
        temp_store=temp_store,
        target_options={
            k: {'compressor': zarr.Blosc(clevel=1), 'write_empty_chunks': True} for k in chunks_dict
        },
        temp_options={k: {'compressor': None, 'write_empty_chunks': True} for k in chunks_dict},
        executor='dask',
    )

    rechunk_plan.execute()

    # consolidate_metadata here since when it comes out of rechunker it isn't consolidated.
    zarr.consolidate_metadata(target_store)
    validate_zarr_store(target_store)

    temp_store.clear()
    return target


@task(log_stdout=True)
def time_summary(ds_path: UPath, freq: str) -> UPath:
    """Prefect task to create resampled data. Takes mean of `tasmax` and `tasmin` and sum of `pr`.

    Parameters
    ----------
    ds_path : UPath
        UPath to input zarr store at daily timestep
    freq : str
        aggregation frequency

    Returns
    -------
    UPath
        Path to resampled dataset.
    """

    ds_hash = str_to_hash(str(ds_path) + freq)
    target = results_dir / 'time_summary' / ds_hash

    if use_cache and is_cached(target):
        print(f'found existing target: {target}')
        return target

    ds = xr.open_zarr(ds_path)

    out_ds = resample_wrapper(ds, freq=freq)

    out_ds.attrs.update({'title': 'time_summary'}, **get_cf_global_attrs(version=version))
    out_ds = set_zarr_encoding(out_ds)
    blocking_to_zarr(ds=out_ds, target=target, validate=True, write_empty_chunks=True)

    return target


@task(log_stdout=True)
def get_weights(*, run_parameters, direction, regrid_method="bilinear"):
    """Retrieve pre-generated regridding weights.

    Parameters
    ----------
    run_parameters : dict
        Dictionary of run parameters
    direction : str
        Direction of regridding.
    regrid_method : str
        Regridding method.

    Returns
    -------
    path : UPath
        Path to weights file.
    """
    weights = pd.read_csv(config.get('weights.gcm_obs_weights.uri'))
    path = (
        weights[
            (weights.source_id == run_parameters.model)
            & (weights.table_id == run_parameters.table_id)
            & (weights.grid_label == run_parameters.grid_label)
            & (weights.regrid_method == regrid_method)
            & (weights.direction == direction)
        ]
        .iloc[0]
        .path
    )
    return path


@task(log_stdout=True)
def get_pyramid_weights(*, run_parameters, levels: int, regrid_method: str = "bilinear"):
    """Retrieve pre-generated regridding pyramids weights.

    Parameters
    ----------
    run_parameters : dict
        Dictionary of run parameters
    levels : int
        Number of levels in the pyramid.
    regrid_method : str
        Regridding method.

    Returns
    -------
    path : UPath
        Path to pyramid weights file.
    """
    weights = pd.read_csv(config.get('weights.downscaled_pyramid_weights.uri'))
    path = (
        weights[(weights.regrid_method == regrid_method) & (weights.levels == levels)].iloc[0].path
    )
    return path


@task(log_stdout=True)
def regrid(
    source_path: UPath,
    target_grid_path: UPath,
    weights_path: UPath = None,
    pre_chunk_def: dict = None,
) -> UPath:
    """Task to regrid a dataset to target grid.

    Parameters
    ----------
    source_path : UPath
        Path to dataset that will be regridded
    target_grid_path : UPath
        Path to template grid dataset
    weights_path : UPath (Optional)
        Path to weights file

    Returns
    -------
    UPath
        Path to regridded output dataset.
    """

    import xesmf as xe

    ds_hash = str_to_hash(str(source_path) + str(target_grid_path))
    target = intermediate_dir / 'regrid' / ds_hash

    if use_cache and is_cached(target):
        print(f'found existing target: {target}')
        return target

    source_ds = xr.open_zarr(source_path)
    target_grid_ds = xr.open_zarr(target_grid_path)

    if pre_chunk_def is not None:
        source_ds = source_ds.chunk(**pre_chunk_def)

    if weights_path:
        from ndpyramid.regrid import _reconstruct_xesmf_weights

        weights = _reconstruct_xesmf_weights(xr.open_zarr(weights_path))
        regridder = xe.Regridder(
            source_ds,
            target_grid_ds,
            weights=weights,
            reuse_weights=True,
            method="bilinear",
            extrap_method="nearest_s2d",
            ignore_degenerate=True,
        )
    else:
        regridder = xe.Regridder(
            source_ds,
            target_grid_ds,
            method="bilinear",
            extrap_method="nearest_s2d",
            ignore_degenerate=True,
        )

    regridded_ds = regridder(source_ds, keep_attrs=True)
    regridded_ds.attrs.update(
        {'title': source_ds.attrs['title']}, **get_cf_global_attrs(version=version)
    )

    regridded_ds = set_zarr_encoding(regridded_ds)
    blocking_to_zarr(ds=regridded_ds, target=target, validate=True, write_empty_chunks=True)
    return target


def _load_coords(ds: xr.Dataset) -> xr.Dataset:
    '''Helper function to explicitly load all dataset coordinates'''
    for var, da in ds.coords.items():
        ds[var] = da.load()
    return ds


def _pyramid_postprocess(dt: dt.DataTree, levels: int, other_chunks: dict = None) -> dt.DataTree:
    '''Postprocess data pyramid

    Adds multiscales metadata and sets Zarr encoding

    Parameters
    ----------
    dt : dt.DataTree
        Input data pyramid
    levels : int
        Number of levels in pyramid
    other_chunks : dict
        Chunks for non-spatial dims

    Returns
    -------
    dt.DataTree
        Updated data pyramid with metadata / encoding set
    '''
    chunks = {"x": PIXELS_PER_TILE, "y": PIXELS_PER_TILE}
    if other_chunks is not None:
        chunks.update(other_chunks)

    for level in range(levels):
        slevel = str(level)
        dt.ds.attrs['multiscales'][0]['datasets'][level]['pixels_per_tile'] = PIXELS_PER_TILE

        # set dataset chunks
        dt[slevel].ds = dt[slevel].ds.chunk(chunks)
        if 'date_str' in dt[slevel].ds:
            dt[slevel].ds['date_str'] = dt[slevel].ds['date_str'].chunk(-1)

        # set dataset encoding
        dt[slevel].ds = set_web_zarr_encoding(
            dt[slevel].ds, codec_config={"id": "zlib", "level": 1}, float_dtype="float32"
        )
        for var in ['time', 'time_bnds']:
            if var in dt[slevel].ds:
                dt[slevel].ds[var].encoding['dtype'] = 'int32'

    # set global metadata
    dt.ds.attrs.update({'title': 'multiscale data pyramid'}, **get_cf_global_attrs(version=version))
    return dt


@task(log_stdout=True)
def pyramid(
    ds_path: UPath, weights_pyramid_path: str = None, levels: int = 2, other_chunks: dict = None
) -> UPath:
    '''Task to create a data pyramid from an xarray Dataset

    Parameters
    ----------
    ds_path : UPath
        Path to input dataset
    weights_pyramid_path : str
        Path to weights pyramid
    levels : int, optional
        Number of levels in pyramid, by default 2
    uri : str, optional
        Path to write output data pyamid to, by default None
    other_chunks : dict
        Chunks for non-spatial dims


    Returns
    -------
    target : UPath
    '''
    ds_hash = str_to_hash(str(ds_path) + str(levels) + str(other_chunks))
    target = results_dir / 'pyramid' / ds_hash

    if use_cache and is_cached(target):
        print(f'found existing target: {target}')
        return target

    ds = xr.open_zarr(ds_path).pipe(_load_coords)

    ds.coords['date_str'] = ds['time'].dt.strftime('%Y-%m-%d').astype('S10')

    ds.attrs.update({'title': ds.attrs['title']}, **get_cf_global_attrs(version=version))
    target_pyramid = dt.open_datatree('az://static/target-pyramid', engine='zarr')
    if weights_pyramid_path is not None:
        weights_pyramid = dt.open_datatree(weights_pyramid_path, engine='zarr')
    else:
        weights_pyramid = None
    # create pyramid
    dta = pyramid_regrid(
        ds,
        target_pyramid=target_pyramid,
        levels=levels,
        weights_pyramid=weights_pyramid,
        regridder_kws={'ignore_degenerate': True},
    )

    dta = _pyramid_postprocess(dta, levels=levels, other_chunks=other_chunks)

    # write to target
    for child in dta.children.values():
        for variable in child.ds.data_vars:
            child[variable].encoding['write_empty_chunks'] = True

    dta.to_zarr(target, mode='w')
    validate_zarr_store(target)
    return target


@task(log_stdout=True)
def run_analyses(ds_path: UPath, run_parameters: RunParameters) -> UPath:
    """Prefect task to run the analyses on results from a downscaling run.

    Parameters
    ----------
    ds_path : UPath
        Path to input dataset
    run_parameters : RunParameters
        Downscaling run parameter container

    Returns
    -------
    PosixPath
        The local location of an executed notebook path.
    """

    import papermill
    from azure.storage.blob import BlobServiceClient, ContentSettings

    from cmip6_downscaling.analysis import metrics

    root = PosixPath(metrics.__file__)
    template_path = root.parent / 'analyses_template.ipynb'
    executed_notebook_path = root.parent / f'analyses_{run_parameters.run_id}.ipynb'
    executed_html_path = root.parent / f'analyses_{run_parameters.run_id}.html'

    parameters = asdict(run_parameters)
    parameters['run_id'] = run_parameters.run_id
    # TODO: figure out how to unpack these fields in the notebook
    # asdict will return lists for train_dates and predict_dates
    # parameters['train_period_start'] = train_period.start
    # parameters['train_period_end'] = train_period.stop
    # parameters['predict_period_start'] = predict_period.start
    # parameters['predict_period_end'] = predict_period.stop

    # execute notebook with papermill
    papermill.execute_notebook(template_path, executed_notebook_path, parameters=parameters)

    # convert from ipynb to html
    # TODO: move this to stand alone function
    # Q: can we control the output path name?
    os.system(f"jupyter nbconvert {executed_notebook_path} --to html")

    # TODO: move to stand alone function
    connection_string = os.getenv('AZURE_STORAGE_CONNECTION_STRING', None)
    if connection_string is not None:
        # if you have a connection_string, copy the html to azure, if not just return
        # because it is already in your local machine
        blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        # TODO: fix b/c the run_id has slashes now!!!
        blob_name = config.get('storage.web_results.blob') / parameters.run_id / 'analyses.html'
        blob_client = blob_service_client.get_blob_client(container='$web', blob=blob_name)
        # clean up before writing
        with contextlib.suppress(Exception):
            blob_client.delete_blob()
        #  need to specify html content type so that it will render and not download
        with open(executed_html_path, "rb") as data:
            blob_client.upload_blob(
                data, content_settings=ContentSettings(content_type='text/html')
            )

    return executed_notebook_path


def _finalize(run_parameters, kind='runs', **paths):
    path_dict = dict(**paths)

    now = datetime.datetime.utcnow().isoformat()
    target1 = results_dir / kind / run_parameters.run_id / f'{now}.json'
    target2 = results_dir / kind / run_parameters.run_id / 'latest.json'
    print(f'finalize 1: {target1}')
    print(f'finalize 2: {target2}')

    out = {'parameters': asdict(run_parameters)}
    out['attrs'] = get_cf_global_attrs(version=version)
    out['datasets'] = {k: str(p) for k, p in path_dict.items()}

    with target1.open(mode='w') as f:
        json.dump(out, f, indent=2)

    with target2.open(mode='w') as f:
        json.dump(out, f, indent=2)


@task(log_stdout=True)
def finalize(run_parameters: RunParameters = None, **paths):
    """Prefect task to finalize the downscaling run.

    Parameters
    ----------
    run_parameters : RunParameters
        Downscaling run parameter container
    paths : dict
        Dictionary of paths to write result file
    """
    _finalize(run_parameters, kind='runs', **paths)


@task(log_stdout=True, trigger=any_failed)
def finalize_on_failure(run_parameters: RunParameters = None, **paths):
    """Prefect task to finalize a failed downscaling run.

    Parameters
    ----------
    run_parameters : RunParameters
        Downscaling run parameter container
    paths : dict
        Dictionary of paths to write to result file
    """

    _finalize(run_parameters, kind='failed-runs', **paths)
