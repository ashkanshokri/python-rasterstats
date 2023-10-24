# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import division
import numpy as np
import warnings
from copy import copy
from affine import Affine
from . import io
from shapely.geometry import shape
from .io import read_features, Raster
from .utils import (rasterize_geom, get_percentile, check_stats,
                    remap_categories, key_assoc_val, boxify_points,
                    rasterize_pctcover_geom, get_latitude_scale,
                    split_geom, VALID_STATS)

def zonal_stats_timeseries(vector, da, *args, dim='time', method='sum', **kwargs):
    """
    
    """

    dim_idx = np.where(np.array(da.dims) != 'time')[0]
    dim_idx = dim_idx.astype(int)
    
    affine = da.rio.transform()
    raster = da.isel(**{dim:0}).values
    o = zonal_stats(vector, raster, affine=affine, states=method,percent_cover_weighting=True, raster_out=True, **kwargs)
    print('.')
    nodata = o[0]['nodata']
    band = o[0]['band']
    rast = io.Raster(da.values, affine, nodata, band)
    l = []
    for v in o:        
        fsrc = rast.read(bounds=v['sub_geom_bounds']).array
        mask = np.broadcast_to(v['mini_raster_array'].mask, fsrc.shape)
        masked = np.ma.MaskedArray(fsrc, mask=mask)
        if method=='sum':
            arr = masked * v['cover_weights']         
            l.append(arr.sum(axis=tuple(dim_idx)).data)
        elif method=='mean':
            arr = masked * v['cover_weights']     
            tmp_numerator = arr.sum(axis=tuple(dim_idx))
            tmp_denominator = (~arr.mask * v['cover_weights']).sum(axis=tuple(dim_idx))
            l.append((tmp_numerator / tmp_denominator).data)
           
    return l



def raster_stats(*args, **kwargs):
    """Deprecated. Use zonal_stats instead."""
    warnings.warn("'raster_stats' is an alias to 'zonal_stats'"
                  " and will disappear in 1.0", DeprecationWarning)
    return zonal_stats(*args, **kwargs)


def zonal_stats(*args, **kwargs):
    """The primary zonal statistics entry point.

    All arguments are passed directly to ``gen_zonal_stats``.
    See its docstring for details.

    The only difference is that ``zonal_stats`` will
    return a list rather than a generator."""
    return list(gen_zonal_stats(*args, **kwargs))


def gen_zonal_stats(
        vectors, raster,
        layer=0,
        band=1,
        nodata=None,
        affine=None,
        stats=None,
        all_touched=False,
        latitude_correction=False,
        percent_cover_selection=None,
        percent_cover_weighting=False,
        percent_cover_scale=None,
        limit=None,
        categorical=False,
        category_map=None,
        add_stats=None,
        zone_func=None,
        raster_out=False,
        prefix=None,
        geojson_out=False, **kwargs):
    """Zonal statistics of raster values aggregated to vector geometries.

    Parameters
    ----------
    vectors: path to an vector source or geo-like python objects

    raster: ndarray or path to a GDAL raster source
        If ndarray is passed, the ``affine`` kwarg is required.

    layer: int or string, optional
        If `vectors` is a path to an fiona source,
        specify the vector layer to use either by name or number.
        defaults to 0

    band: int, optional
        If `raster` is a GDAL source, the band number to use (counting from 1).
        defaults to 1.

    nodata: float, optional
        If `raster` is a GDAL source, this value overrides any NODATA value
        specified in the file's metadata.
        If `None`, the file's metadata's NODATA value (if any) will be used.
        defaults to `None`.

    affine: Affine instance
        required only for ndarrays, otherwise it is read from src

    stats:  list of str, or space-delimited str, optional
        Which statistics to calculate for each zone.
        All possible choices are listed in ``utils.VALID_STATS``.
        defaults to ``DEFAULT_STATS``, a subset of these.

    all_touched: bool, optional
        Whether to include every raster cell touched by a geometry, or only
        those having a center point within the polygon.
        defaults to `False`

    latitude_correction: bool, optional
        * For use with WGS84 data only.
        * Only applies to "mean" stat.
        Weights cell values when generating statistics based on latitude
        (using haversine function) in order to account for actual area
        represented by pixel cell.

    percent_cover_selection: float, optional
        Include only raster cells that have at least the given percent
        covered by the vector feature. Requires percent_cover_scale argument
        be used to specify scale at which to generate percent coverage
        estimates

    percent_cover_weighting: bool, optional
        whether or not to use percent coverage of cells during calculations
        to adjust stats (only applies to mean, count and sum)

    percent_cover_scale: int, optional
        Scale used when generating percent coverage estimates of each
        raster cell by vector feature. Percent coverage is generated by
        rasterizing the feature at a finer resolution than the raster
        (based on percent_cover_scale value) then using a summation to aggregate
        to the raster resolution and dividing by the square of percent_cover_scale
        to get percent coverage value for each cell. Increasing percent_cover_scale
        will increase the accuracy of percent coverage values. Three orders
        magnitude finer resolution (percent_cover_scale=1000) is usually enough to
        get coverage estimates with <1% error in individual edge cells coverage
        estimates, but the limited meaningful accuracy improvements this results in
        (compared to lower percent_cover_scale values, e.g., 10) are generally
        not worth the signficant additional computation cost and processing time.
        Values (e.g., percent_cover_scale=10) are often sufficient
        (<10% edge cell error) and require far less memory and time to run.

    limit: int
        maximum number of pixels allowed to be read from raster based on
        feature bounds. Geometries which will result in reading a larger
        number of pixels will be split into smaller geometries and then
        aggregated (note: some stats and options cannot be used along with
        `limit`. Useful when dealing with vector data containing
        large features and raster with a fine resolution to prevent
        memory errors. If the limit value is None (default) or 0
        geometries will never be split. Using the `limit` option without
        also using the `percent_cover_weighting`option may result in less
        accurate statistics due to generating additional polygon edges when
        splitting geometries. When using percent_cover_scale of 10, a limit
        of 5 million pixels is generally reasonably quick and should run
        with <4GB of RAM.

    categorical: bool, optional

    category_map: dict
        A dictionary mapping raster values to human-readable categorical names.
        Only applies when categorical is True

    add_stats: dict
        with names and functions of additional stats to compute, optional

    zone_func: callable
        function to apply to zone ndarray prior to computing stats

    raster_out: boolean
        Include the masked numpy array for each feature?, optional

        Each feature dictionary will have the following additional keys:
        mini_raster_array: The clipped and masked numpy array
        mini_raster_affine: Affine transformation
        mini_raster_nodata: NoData Value

    prefix: string
        add a prefix to the keys (default: None)

    geojson_out: boolean
        Return list of GeoJSON-like features (default: False)
        Original feature geometry and properties will be retained
        with zonal stats appended as additional properties.
        Use with `prefix` to ensure unique and meaningful property names.

    Returns
    -------
    generator of dicts (if geojson_out is False)
        Each item corresponds to a single vector feature and
        contains keys for each of the specified stats.

    generator of geojson features (if geojson_out is True)
        GeoJSON-like Feature as python dict
    """
    stats, run_count = check_stats(stats, categorical)

    # Handle 1.0 deprecations
    transform = kwargs.get('transform')
    if transform:
        warnings.warn("GDAL-style transforms will disappear in 1.0. "
                      "Use affine=Affine.from_gdal(*transform) instead",
                      DeprecationWarning)
        if not affine:
            affine = Affine.from_gdal(*transform)

    cp = kwargs.get('copy_properties')
    if cp:
        warnings.warn("Use `geojson_out` to preserve feature properties",
                      DeprecationWarning)

    band_num = kwargs.get('band_num')
    if band_num:
        warnings.warn("Use `band` to specify band number", DeprecationWarning)
        band = band_num


    # -------------------------------------------------------------------------
    # make sure feature split/aggregations will work with options provided


    limit = None if not limit else limit

    if limit is not None:

        try:
            limit = int(limit)
        except ValueError:
            raise ValueError('`limit` must be a number (Input: {0}, {1})'.format(type(limit), limit))

        invalid_limit_stats = [
            'minority', 'majority', 'median', 'std', 'unique'
        ] + [s for s in stats if s.startswith('percentile_')]

        invalid_limit_conditions = (
            any([i in invalid_limit_stats for i in stats])
            or add_stats is not None
            or raster_out
        )
        if invalid_limit_conditions:
            raise Exception("Cannot use `limit` to split geometries when using "
                            "`add_stats` or `raster_out` options")

        # need count for sub geoms to calculate weighted mean
        use_temp_count = False
        if 'mean' in stats and not 'count' in stats:
            use_temp_count = True
            stats.append('count')


    # -------------------------------------------------------------------------
    # check inputs related to percent coverage
    percent_cover = False
    if percent_cover_weighting or percent_cover_selection is not None:
        percent_cover = True
        if percent_cover_scale is None:
            warnings.warn('No value for `percent_cover_scale` was given. '
                          'Using default value of 10.')
            percent_cover_scale = 10

        if percent_cover_scale > 1000:
            warnings.warn('Using a value for `percent_cover_scale` over 1000 '
                          'will result in significantly slower processing '
                          'with little to no meaningful improvements in '
                          'accuracy. (Maximum suggested value is 100, and '
                          'for most cases 10 is sufficient.')
        try:
            if percent_cover_scale != int(percent_cover_scale):
                warnings.warn('Value for `percent_cover_scale` given ({0}) '
                              'was converted to int ({1}) but does not '
                              'match original value'.format(
                                percent_cover_scale, int(percent_cover_scale)))

            percent_cover_scale = int(percent_cover_scale)

            if percent_cover_scale <= 1:
                raise Exception('Value for `percent_cover_scale` must be '
                                'greater than one ({0})'.format(
                                    percent_cover_scale))

        except:
            raise ValueError('Invalid value for `percent_cover_scale` '
                             'provided ({0}). Must be type int.'.format(
                                percent_cover_scale))

        if percent_cover_selection is not None:
            try:
                percent_cover_selection = float(percent_cover_selection)
            except:
                raise ValueError('Invalid value for `percent_cover_selection` '
                                 'provided ({0}). Must be able to be converted '
                                 'to a float.'.format(percent_cover_selection))

        if not all_touched:
            warnings.warn('The `all_touched` was not enabled, but an option '
                          'requiring percent_cover calculations was selected. '
                          'We suggest enabling `all_touched` when using '
                          'either `percent_cover_weighting` or '
                          '`percent_cover_selection`.')


    with Raster(raster, affine, nodata, band) as rast:
        features_iter = read_features(vectors, layer)
        for _, feat in enumerate(features_iter):
            geom = shape(feat['geometry'])

            if 'Point' in geom.geom_type:
                geom = boxify_points(geom, rast)
                percent_cover = percent_cover_weighting = False


            # -----------------------------------------------------------------
            # build geom_list (split geoms if needed)

            if limit is None:
                geom_list = [geom]

            else:
                pixel_size = rast.affine[0]
                origin = (rast.affine[2], rast.affine[5])
                geom_list = split_geom(geom, limit, pixel_size, origin=origin)


            # -----------------------------------------------------------------
            # run sub geom extracts

            for ix, sub_geom_box in enumerate(geom_list):

                sub_geom_bounds = tuple(sub_geom_box.bounds)

                fsrc = rast.read(bounds=sub_geom_bounds)
                

                # rasterized geometry
                if percent_cover:
                    cover_weights = rasterize_pctcover_geom(
                        geom, shape=fsrc.shape, affine=fsrc.affine,
                        scale=percent_cover_scale,
                        all_touched=all_touched)
                    rv_array = cover_weights > (percent_cover_selection or 0)
                else:
                    rv_array = rasterize_geom(
                        geom, shape=fsrc.shape, affine=fsrc.affine,
                        all_touched=all_touched)


                # nodata mask
                isnodata = (fsrc.array == fsrc.nodata)

                # add nan mask (if necessary)
                has_nan = (np.issubdtype(fsrc.array.dtype, float)
                    and np.isnan(fsrc.array.min()))
                if has_nan:
                    isnodata = (isnodata | np.isnan(fsrc.array))

                # Mask the source data array
                # mask everything that is not a valid value or not within our geom

                masked = np.ma.MaskedArray(
                    fsrc.array,
                    mask=(isnodata | ~rv_array))
                
                if fsrc.array.shape[0]>200000:
                    print(fsrc.array)
                    
                    assert False
                # execute zone_func on masked zone ndarray
                if zone_func is not None:
                    if not callable(zone_func):
                        raise TypeError(('zone_func must be a callable '
                                         'which accepts function a '
                                         'single `zone_array` arg.'))
                    zone_func(masked)

                if latitude_correction and 'mean' in stats:
                    latitude_scale = [
                        get_latitude_scale(fsrc.affine[5] - abs(fsrc.affine[4]) * (0.5 + i))
                        for i in range(fsrc.shape[0])
                    ]

                if masked.compressed().size == 0:
                    # nothing here, fill with None and move on
                    sub_feature_stats = dict([(stat, None) for stat in stats])
                    if 'count' in stats:  # special case, zero makes sense here
                        sub_feature_stats['count'] = 0
                else:
                    if run_count:
                        keys, counts = np.unique(masked.compressed(), return_counts=True)
                        pixel_count = dict(zip([np.asscalar(k) for k in keys],
                                               [np.asscalar(c) for c in counts]))

                    if categorical:
                        sub_feature_stats = dict(pixel_count)
                        if category_map:
                            sub_feature_stats = remap_categories(category_map, sub_feature_stats)
                    else:
                        sub_feature_stats = {}

                    if 'count' in stats:
                        if percent_cover_weighting:
                            sub_feature_stats['count'] = float(np.sum(~masked.mask * cover_weights))
                        else:
                            sub_feature_stats['count'] = int(masked.count())

                    if 'sum' in stats:
                        if percent_cover_weighting:
                            sub_feature_stats['sum'] = float(np.sum(masked * cover_weights))
                        else:
                            sub_feature_stats['sum'] = float(masked.sum())

                    if 'mean' in stats:
                        if percent_cover_weighting and latitude_correction:

                            tmp_numerator = np.sum((masked.T * latitude_scale).T * cover_weights)
                            tmp_denominator = np.sum((~masked.mask.T * latitude_scale).T * cover_weights)
                            sub_feature_stats['mean'] = float(tmp_numerator / tmp_denominator)
                            sub_feature_stats['latitude_correction'] = tmp_denominator / np.sum(~masked.mask * cover_weights)

                        elif percent_cover_weighting:

                            tmp_numerator = np.sum(masked * cover_weights)
                            tmp_denominator = np.sum(~masked.mask * cover_weights)
                            sub_feature_stats['mean'] = float(tmp_numerator / tmp_denominator)

                        elif latitude_correction:

                            tmp_numerator =  np.sum((masked.T * latitude_scale).T)
                            tmp_denominator = np.sum(np.sum(~masked.mask, axis=1) * latitude_scale)
                            sub_feature_stats['mean'] = float(tmp_numerator / tmp_denominator)
                            sub_feature_stats['latitude_correction'] = tmp_denominator / np.sum(~masked.mask)

                        else:
                            sub_feature_stats['mean'] = float(masked.mean())

                    if 'min' in stats:
                        sub_feature_stats['min'] = float(masked.min())
                    if 'max' in stats:
                        sub_feature_stats['max'] = float(masked.max())
                    if 'range' in stats:
                        rmin = float(masked.min())
                        rmax = float(masked.max())
                        sub_feature_stats['min'] = rmin
                        sub_feature_stats['max'] = rmax
                        sub_feature_stats['range'] = rmax - rmin

                    if 'std' in stats:
                        sub_feature_stats['std'] = float(masked.std())
                    if 'median' in stats:
                        sub_feature_stats['median'] = float(np.median(masked.compressed()))
                    if 'majority' in stats:
                        sub_feature_stats['majority'] = float(key_assoc_val(pixel_count, max))
                    if 'minority' in stats:
                        sub_feature_stats['minority'] = float(key_assoc_val(pixel_count, min))
                    if 'unique' in stats:
                        sub_feature_stats['unique'] = len(list(pixel_count.keys()))

                    for pctile in [s for s in stats if s.startswith('percentile_')]:
                        q = get_percentile(pctile)
                        pctarr = masked.compressed()
                        sub_feature_stats[pctile] = np.percentile(pctarr, q)

                if 'nodata' in stats or 'nan' in stats:
                    featmasked = np.ma.MaskedArray(fsrc.array, mask=(~rv_array))
                    if 'nodata' in stats:
                        nodata_match = (featmasked == fsrc.nodata)
                        if nodata_match.count() == 0:
                            sub_feature_stats['nodata'] = 0
                        else:
                            sub_feature_stats['nodata'] = float(nodata_match.sum())
                    if 'nan' in stats:
                        sub_feature_stats['nan'] = float(np.isnan(featmasked).sum()) if has_nan else 0

                if add_stats is not None:
                    for stat_name, stat_func in add_stats.items():
                        sub_feature_stats[stat_name] = stat_func(masked)

                if raster_out:
                    sub_feature_stats['mini_raster_array'] = masked
                    sub_feature_stats['mini_raster_affine'] = fsrc.affine
                    sub_feature_stats['mini_raster_nodata'] = fsrc.nodata
                    sub_feature_stats['cover_weights'] = cover_weights
                    sub_feature_stats['sub_geom_box'] = sub_geom_box
                    sub_feature_stats['sub_geom_bounds'] = sub_geom_bounds
                    sub_feature_stats['nodata'] = nodata
                    sub_feature_stats['band'] = band


                # -----------------------------------------------------------------
                # aggregate sub geom extracts

                if ix == 0:
                    feature_stats = copy(sub_feature_stats)
                else:

                    tmp_feature_stats = copy(feature_stats)
                    feature_stats = {}
                    sub_feature_stats_list = [tmp_feature_stats, sub_feature_stats]

                    if 'count' in stats:
                        feature_stats['count'] = sum([i['count'] for i in sub_feature_stats_list])

                    if 'sum' in stats:
                        vals = [i['sum'] for i in sub_feature_stats_list if 'sum' in i]
                        feature_stats['sum'] = sum(vals) if vals else None

                    if 'mean' in stats:
                        valid_means = [i['mean'] for i in sub_feature_stats_list if i['mean'] is not None]
                        if not valid_means:
                            feature_stats['mean'] = None
                        elif latitude_correction:
                            feature_stats['latitude_correction'] = sum([i['count'] * i['latitude_correction'] for i in sub_feature_stats_list if i['count']]) / sum([i['count'] for i in sub_feature_stats_list if i['count']])
                            feature_stats['mean'] = sum([i['mean'] * i['count'] * i['latitude_correction'] for i in sub_feature_stats_list if i['count']]) / sum([i['count'] * i['latitude_correction'] for i in sub_feature_stats_list if i['count']])
                        else:
                            feature_stats['mean'] = sum([i['mean'] * i['count'] for i in sub_feature_stats_list if i['count']]) / sum([i['count'] for i in sub_feature_stats_list if i['count']])

                    if 'min' in stats:
                        vals = [i['min'] for i in sub_feature_stats_list if i['min'] is not None]
                        feature_stats['min'] = min(vals) if vals else None
                    if 'max' in stats:
                        vals = [i['max'] for i in sub_feature_stats_list if i['max'] is not None]
                        feature_stats['max'] = max(vals) if vals else None
                    if 'range' in stats:
                        min_vals = [i['min'] for i in sub_feature_stats_list if i['min'] is not None]
                        max_vals = [i['max'] for i in sub_feature_stats_list if i['max'] is not None]
                        rmin = min(vals) if vals else None
                        rmax = max(vals) if vals else None
                        feature_stats['range'] = rmax - rmin if rmin is not None else None

                    if 'nodata' in stats:
                        feature_stats['nodata'] = sum([i['nodata'] for i in sub_feature_stats_list])
                    if 'nan' in stats:
                        feature_stats['nan'] = sum([i['nan'] for i in sub_feature_stats_list])

                    if categorical:
                        for sub_stats in sub_feature_stats_list:
                            for field in sub_stats:
                                if field not in VALID_STATS:
                                    if field not in feature_stats:
                                        feature_stats[field] = sub_stats[field]
                                    else:
                                        feature_stats[field] += sub_stats[field]


            if ix == 0:
                if 'range' in stats and not 'min' in stats and 'min' in feature_stats:
                    del feature_stats['min']
                if 'range' in stats and not 'max' in stats and 'max' in feature_stats:
                    del feature_stats['max']

            if latitude_correction and 'mean' in stats and 'latitude_correction' in feature_stats:
                del feature_stats['latitude_correction']

            if limit is not None and use_temp_count:
                del feature_stats['count']

            if prefix is not None:
                prefixed_feature_stats = {}
                for key, val in feature_stats.items():
                    newkey = "{}{}".format(prefix, key)
                    prefixed_feature_stats[newkey] = val
                feature_stats = prefixed_feature_stats

            if geojson_out:
                for key, val in feature_stats.items():
                    if 'properties' not in feat:
                        feat['properties'] = {}
                    feat['properties'][key] = val
                yield feat
            else:
                yield feature_stats
