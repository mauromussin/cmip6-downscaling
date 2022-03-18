import os
from dataclasses import asdict
from pathlib import PosixPath
from typing import Union

import datatree as dt
import fsspec
import papermill as pm
import rechunker
import xarray as xr
import xesmf as xe
import zarr
from azure.storage.blob import BlobServiceClient, ContentSettings
from carbonplan_data.metadata import get_cf_global_attrs
from carbonplan_data.utils import set_zarr_encoding
from ndpyramid import pyramid_regrid
from prefect import task
from upath import UPath
from xarray_schema import DataArraySchema, DatasetSchema
from xarray_schema.base import SchemaError

from cmip6_downscaling import config, version
from cmip6_downscaling.data.cmip import load_cmip
from cmip6_downscaling.data.observations import open_era5
from cmip6_downscaling.data.utils import subset_dataset
from cmip6_downscaling.methods.common.utils import calc_auspicious_chunks_dict

from .containers import RunParameters

PIXELS_PER_TILE = 128

intermediate_dir = UPath(config.get("storage.intermediate.uri"))
scratch_dir = UPath(config.get("storage.scratch.uri"))
use_cache = config.get('run_options.use_cache')


@task
def make_run_parameters(**kwargs) -> RunParameters:
    """Prefect task wrapper for RunParameters"""
    return RunParameters(**kwargs)


@task
def get_obs(run_parameters: RunParameters) -> UPath:

    target = (
        intermediate_dir
        / "get_obs"
        / "{obs}_{variable}_{latmin}_{latmax}_{lonmin}_{lonmax}_{train_dates[0]}_{train_dates[1]}".format(
            **asdict(run_parameters)
        )
    )
    if use_cache and (target / '.zmetadata').exists():
        print(f'found existing target: {target}')
        return target

    ds = open_era5(run_parameters.variable, run_parameters.train_period)
    subset = subset_dataset(
        ds,
        run_parameters.variable,
        run_parameters.train_period.time_slice,
        run_parameters.bbox,
        chunking_schema={'time': 365, 'lat': 150, 'lon': 150},
    )
    del subset[run_parameters.variable].encoding['chunks']

    subset.attrs.update(**get_cf_global_attrs(version=version))
    subset.to_zarr(target, mode='w')
    return target


@task
def get_experiment(run_parameters: RunParameters, time_subset: str) -> UPath:
    time_period = getattr(run_parameters, time_subset)
    target = (
        intermediate_dir
        / "get_experiment"
        / "{model}_{scenario}_{variable}_{latmin}_{latmax}_{lonmin}_{lonmax}_{time_period.start}_{time_period.stop}".format(
            time_period=time_period, **asdict(run_parameters)
        )
    )
    if use_cache and (target / '.zmetadata').exists():
        print(f'found existing target: {target}')
        return target

    ds = load_cmip(source_ids=run_parameters.model, variable_ids=run_parameters.variable)
    subset = subset_dataset(ds, run_parameters.variable, time_period, run_parameters.bbox)
    # Note: dataset is chunked into time:365 chunks to standardize leap-year chunking.

    subset = subset.chunk({'time': 365})
    del subset[run_parameters.variable].encoding['chunks']

    subset.attrs.update(**get_cf_global_attrs(version=version))
    subset.to_zarr(target, mode='w')
    return target


@task
def rechunk(path: UPath, chunking_pattern: Union[str, UPath] = None, max_mem: str = "2GB") -> UPath:
    """Use `rechunker` package to adjust chunks of dataset to a form
    conducive for your processing.

    Parameters
    ----------
    path : UPath
        path to zarr store
    chunking_pattern : str or UPath
        The pattern of chunking you want to use.
    max_mem : str
        The memory available for rechunking steps. Must look like "2GB". Optional, default is 2GB.

    Returns
    -------
    target : UPath
        Path to rechunked dataset
    """

    if isinstance(chunking_pattern, str):
        pattern_string = chunking_pattern
    elif isinstance(chunking_pattern, UPath):
        # when you pass a dataset the chunking will match the chunking of that dataset
        # so we'll call it "matched". this is ambiguous - like it could be chunked to match
        # any number of patterns but it is sufficient for this implementation.
        pattern_string = 'matched'
    else:
        raise NotImplementedError(
            "Not a valid chunking approach. Try passing either `full_space` or `full_time`, or a template chunked dataset."
        )
    target = intermediate_dir / "rechunk" / (pattern_string + path.path.replace("/", "_"))
    path_tmp = scratch_dir / "rechunk" / (pattern_string + path.path.replace("/", "_"))
    print(target)
    target_store = fsspec.get_mapper(str(target))
    temp_store = fsspec.get_mapper(str(path_tmp))

    if use_cache and (target / '.zmetadata').exists():
        print(f'found existing target: {target}')
        # if we wanted to check that it was chunked correctly we could put this down below where
        # the target_schema is validated. but that requires us going through the development
        # of the schema would just hurt performance likely unnecessarily.
        # nevertheless, as future note: if we encounter chunk issues i suggest putting a schema check here
        return target
    # if a cached target isn't found we'll go through the rechunking step
    # open the zarr group
    group = zarr.open_consolidated(path)
    # open the dataset to access the coordinates
    ds = xr.open_zarr(path)
    example_var = list(ds.data_vars)[0]
    # based upon whether you want to chunk along space or time, take the dataset and calculate
    # an optimal chunking schema for processing.
    if isinstance(chunking_pattern, str):
        chunk_dims = config.get(f"chunk_dims.{chunking_pattern}")
        chunk_def = calc_auspicious_chunks_dict(ds[example_var], chunk_dims=chunk_dims)

    elif isinstance(chunking_pattern, UPath):
        template_ds = xr.open_dataset(chunking_pattern)
        # define the chunk definition
        chunk_def = {
            'time': min(template_ds.chunks['time'][0], len(ds.time)),
            'lat': min(template_ds.chunks['lat'][0], len(ds.lat)),
            'lon': min(template_ds.chunks['lon'][0], len(ds.lon)),
        }
    # initialize the chunks_dict that you'll pass in, filling the coordinates with
    # `None`` because you don't want to rechunk the coordinate arrays
    chunks_dict = {
        'time': None,
        'lon': None,
        'lat': None,
    }

    for var in ds.data_vars:
        chunks_dict[var] = chunk_def
    # now that you have your chunks_dict you can check that the dataset at `path`
    # you're passing in doesn't already match that schema. because if so, we don't
    # need to bother with rechunking and we'll skip it!
    schema_dict = {}
    for var in ds.data_vars:
        schema_dict[var] = DataArraySchema(chunks=chunk_def)
    target_schema = DatasetSchema(schema_dict)
    try:
        # check to see if the initial dataset already matches the schema, in which case just
        # return the initial path and work with that
        target_schema.validate(ds)
        return path
    except SchemaError:

        rechunk_plan = rechunker.rechunk(
            source=group,
            target_chunks=chunks_dict,
            max_mem=max_mem,
            target_store=target_store,
            temp_store=temp_store,
        )

        rechunk_plan.execute()

        # consolidate_metadata here since when it comes out of rechunker it isn't consolidated.
        zarr.consolidate_metadata(target_store)
        temp_store.clear()

        return target


@task
def monthly_summary(ds_path: UPath, run_parameters: RunParameters) -> UPath:

    target = intermediate_dir / "monthly_summary" / run_parameters.run_id
    if use_cache and (target / '.zmetadata').exists():
        print(f'found existing target: {target}')
        return target

    ds = xr.open_zarr(ds_path)

    out_ds = xr.Dataset()
    for var in ds:
        if var in ['tasmax', 'tasmin']:
            out_ds[var] = ds[var].resample(time='1MS').mean(dim='time')
        elif var in ['pr']:
            out_ds[var] = ds[var].resample(time='1MS').sum(dim='time')
        else:
            print(f'{var} not implemented')

    out_ds.attrs.update(**get_cf_global_attrs(version=version))
    out_ds.to_zarr(target, mode='w')

    return target


@task
def annual_summary(ds_path: UPath, run_parameters: RunParameters) -> UPath:

    target = intermediate_dir / "annual_summary" / run_parameters.run_id
    if use_cache and (target / '.zmetadata').exists():
        print(f'found existing target: {target}')
        return target

    ds = xr.open_zarr(ds_path)

    out_ds = xr.Dataset()
    for var in ds:
        if var in ['tasmax', 'tasmin']:
            out_ds[var] = ds[var].resample(time='YS').mean()
        elif var in ['pr']:
            out_ds[var] = ds[var].resample(time='YS').sum()
        else:
            print(f'{var} not implemented')

    out_ds.attrs.update(**get_cf_global_attrs(version=version))
    out_ds.to_zarr(target, mode='w')

    return target


@task(tags=['dask-resource:TASKSLOTS=1'], log_stdout=True)
def regrid(source_path: UPath, target_grid_path: UPath) -> UPath:

    target = (
        intermediate_dir
        / "regrid"
        / (source_path.path.replace('/', '_') + '_' + target_grid_path.path.replace('/', '_'))
    )

    if use_cache and (target / '.zmetadata').exists():
        print(f'found existing target: {target}')
        return target
    source_ds = xr.open_zarr(source_path)
    target_grid_ds = xr.open_zarr(target_grid_path)

    regridder = xe.Regridder(source_ds, target_grid_ds, "bilinear", extrap_method="nearest_s2d")
    regridded_ds = regridder(source_ds)
    regridded_ds.attrs.update(**get_cf_global_attrs(version=version))
    regridded_ds.to_zarr(target, mode='w')

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
        dt[slevel].ds = set_zarr_encoding(
            dt[slevel].ds, codec_config={"id": "zlib", "level": 1}, float_dtype="float32"
        )
        for var in ['time', 'time_bnds']:
            if var in dt[slevel].ds:
                dt[slevel].ds[var].encoding['dtype'] = 'int32'

    # set global metadata
    dt.ds.attrs.update(**get_cf_global_attrs(version=version))
    return dt


@task(log_stdout=True, tags=['dask-resource:TASKSLOTS=1'])
def pyramid(ds_path: UPath, levels: int = 2, other_chunks: dict = None) -> UPath:
    '''Task to create a data pyramid from an xarray Dataset

    Parameters
    ----------
    ds_path : UPath
        Path to input dataset
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

    target = intermediate_dir / "pyramid" / ds_path.path.replace('/', '_')

    if use_cache and (target / '.zmetadata').exists():
        print(f'found existing target: {target}')
        return target

    ds = xr.open_zarr(ds_path).pipe(_load_coords)

    ds.coords['date_str'] = ds['time'].dt.strftime('%Y-%m-%d').astype('S10')

    # create pyramid
    dta = pyramid_regrid(ds, target_pyramid=None, levels=levels)

    # postprocess
    dta = _pyramid_postprocess(dta, levels, other_chunks=other_chunks)

    # write to target
    mapper = fsspec.get_mapper(target)
    dta.to_zarr(mapper, mode='w')
    return target


@task
def run_analyses(ds_path: UPath, run_parameters: RunParameters) -> UPath:
    """Prefect task to run the analyses on results from a downscaling run.

    Parameters
    ----------
    ds_path : UPath
    run_parameters : RunParameters
        Downscaling run parameter container

    Returns
    -------
    PosixPath
        The local location of an executed notebook path.
    """

    from cmip6_downscaling.analysis import metrics

    root = PosixPath(metrics.__file__)
    template_path = root.parent / 'analyses_template.ipynb'
    executed_notebook_path = root.parent / f'analyses_{run_parameters.run_id}.ipynb'
    executed_html_path = root.parent / f'analyses_{run_parameters.run_id}.html'

    parameters = asdict(run_parameters)

    # TODO: figure out how to unpack these fields in the notebook
    # asdict will return lists for train_dates and predict_dates
    # parameters['train_period_start'] = train_period.start
    # parameters['train_period_end'] = train_period.stop
    # parameters['predict_period_start'] = predict_period.start
    # parameters['predict_period_end'] = predict_period.stop

    # execute notebook with papermill
    pm.execute_notebook(template_path, executed_notebook_path, parameters=parameters)

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
        try:
            blob_client.delete_blob()
        except:  # TODO: raise specific error
            pass

        #  need to specify html content type so that it will render and not download
        with open(executed_html_path, "rb") as data:
            blob_client.upload_blob(
                data, content_settings=ContentSettings(content_type='text/html')
            )

    return executed_notebook_path
