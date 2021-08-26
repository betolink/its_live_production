"""
Script to generate catalog geojson file for ITS_LIVE granule dataset.

Authors: Mark Fahnestock, Masha Liukis
"""

import argparse
import dask
from dask.diagnostics import ProgressBar
from datetime import datetime
import geojson
import h5py
import json
import logging
import numpy as np
import os
import psutil
import pyproj
import s3fs
import sys
import time
from tqdm import tqdm
import xarray as xr


# Date format as it appears in granules filenames of optical format:
# LC08_L1TP_011002_20150821_20170405_01_T1_X_LC08_L1TP_011002_20150720_20170406_01_T1_G0240V01_P038.nc
DATE_FORMAT = "%Y%m%d"

# Date and time format as it appears in granules filenames or radar format:
# S1A_IW_SLC__1SSH_20170221T204710_20170221T204737_015387_0193F6_AB07_X_S1B_IW_SLC__1SSH_20170227T204628_20170227T204655_004491_007D11_6654_G0240V02_P094.nc
DATE_TIME_FORMAT = "%Y%m%dT%H%M%S"

def get_tokens_from_filename(filename):
    """
    Extract acquisition/processing dates and path/row for two images from the
    optical granule filename, or start/end date/time and product unique ID for
    radar granule filename.
    """
    # Optical format granules have different file naming convention than radar
    # format granules
    is_optical = True
    url_files = os.path.basename(filename).split('_X_')

    # Get tokens for the first image name
    url_tokens = url_files[0].split('_')

    if len(url_tokens) < 9:
        # Optical format granule
        # Get acquisition/processing dates and path&row for both images
        first_date_1 = datetime.strptime(url_tokens[3], DATE_FORMAT)
        second_date_1 = datetime.strptime(url_tokens[4], DATE_FORMAT)
        key_1 = url_tokens[2]

        url_tokens = url_files[1].split('_')
        first_date_2 = datetime.strptime(url_tokens[3], DATE_FORMAT)
        second_date_2 = datetime.strptime(url_tokens[4], DATE_FORMAT)
        key_2 = url_tokens[2]

    else:
        # Radar format granule
        # Get start/end date/time and product unique ID for both images
        is_optical = False

        url_tokens = url_files[0].split('_')
        # Start date and time
        first_date_1 = datetime.strptime(url_tokens[-5], DATE_TIME_FORMAT)
        # Stop date and time
        second_date_1 = datetime.strptime(url_tokens[-4], DATE_TIME_FORMAT)
        # Product unique identifier
        key_1 = url_tokens[-1]

        # Get tokens for the second image name: there are two extra tokens
        # at the end of the filename which are specific to ITS_LIVE filename
        url_tokens = url_files[1].split('_')
        # Start date and time
        first_date_2 = datetime.strptime(url_tokens[-7], DATE_TIME_FORMAT)
        # Stop date and time
        second_date_2 = datetime.strptime(url_tokens[-6], DATE_TIME_FORMAT)
        # Product unique identifier
        key_2 = url_tokens[-3]

    return is_optical, first_date_1, second_date_1, key_1, first_date_2, second_date_2, key_2

def skip_duplicate_granules(found_urls: list):
    """
    Skip duplicate granules (the ones that have earlier processing date(s)).
    """
    # Need to remove duplicate granules for the middle date: some granules
    # have newer processing date, keep those.
    keep_urls = {}
    skipped_double_granules = []

    for each_url in tqdm(found_urls, ascii=True, desc='Skipping duplicate granules...'):
        # Extract acquisition and processing dates for optical granule,
        # start/end date/time and product unique ID for radar granule
        is_optical, url_acq_1, url_proc_1, key_1, url_acq_2, url_proc_2, key_2 = \
            get_tokens_from_filename(each_url)

        if is_optical:
            # Acquisition time and path/row of images should be identical for
            # duplicate granules
            granule_id = '_'.join([
                url_acq_1.strftime(DATE_FORMAT),
                key_1,
                url_acq_2.strftime(DATE_FORMAT),
                key_2
            ])

        else:
            # Start/stop date/time of both images
            granule_id = '_'.join([
                url_acq_1.strftime(DATE_TIME_FORMAT),
                url_proc_1.strftime(DATE_TIME_FORMAT),
                url_acq_2.strftime(DATE_TIME_FORMAT),
                url_proc_2.strftime(DATE_TIME_FORMAT),
            ])

        # There is a granule for the mid_date already:
        # * For radar granule: issue a warning reporting product unique ID for duplicate granules
        # * For optical granule: check which processing time is newer,
        #                        keep the one with newer processing date
        if granule_id in keep_urls:
            if not is_optical:
                # Radar format granule, just issue a warning
                all_urls = ' '.join(keep_urls[granule_id])
                logging.info(f"WARNING: multiple granules are detected for {each_url}: {all_urls}")
                keep_urls[granule_id].append(each_url)
                continue

            # Process optical granule
            # Flag if newly found URL should be kept
            keep_found_url = False

            for found_url in keep_urls[granule_id]:
                # Check already found URLs for processing time
                _, _, found_proc_1, _, _, found_proc_2, _ = \
                    get_tokens_from_filename(found_url)

                # If both granules have identical processing time,
                # keep them both - granules might be in different projections,
                # any other than target projection will be handled later
                if url_proc_1 == found_proc_1 and \
                   url_proc_2 == found_proc_2:
                    keep_urls[granule_id].append(each_url)
                    keep_found_url = True
                    break

            # There are no "identical" (same acquision and processing times)
            # granules to "each_url", check if new granule has newer processing dates
            if not keep_found_url:
                # Check if any of the found URLs have older processing time
                # than newly found URL
                remove_urls = []
                for found_url in keep_urls[granule_id]:
                    # Check already found URL for processing time
                    _, _, found_proc_1, _, _, found_proc_2, _ = \
                        get_tokens_from_filename(found_url)

                    if url_proc_1 >= found_proc_1 and \
                       url_proc_2 >= found_proc_2:
                        # The granule will need to be replaced with a newer
                        # processed one
                        remove_urls.append(found_url)

                    elif url_proc_1 > found_proc_1:
                        # There are few cases when proc_1 is newer in
                        # each_url and proc_2 is newer in found_url, then
                        # keep the granule with newer proc_1
                        remove_urls.append(found_url)

                if len(remove_urls):
                    # Some of the URLs need to be removed due to newer
                    # processed granule
                    logging.info(f"Skipping {remove_urls} in favor of new {each_url}")
                    skipped_double_granules.extend(remove_urls)

                    # Remove older processed granules based on dates for "each_url"
                    keep_urls[granule_id][:] = [each for each in keep_urls[granule_id] if each not in remove_urls]
                    # Add new granule with newer processing date
                    keep_urls[granule_id].append(each_url)

                else:
                    # New granule has older processing date, don't include
                    logging.info(f"Skipping new {each_url} in favor of {keep_urls[granule_id]}")
                    skipped_double_granules.append(each_url)

        else:
            # This is a granule for new ID, append it to URLs to keep
            keep_urls.setdefault(granule_id, []).append(each_url)

    granules = []
    for each in keep_urls.values():
        granules.extend(each)

    logging.info(f"Keeping {len(granules)} unique granules, skipping {len(skipped_double_granules)} granules")
    return granules, skipped_double_granules


class memtracker:

    def __init__(self, include_time=True):
        self.output_time = include_time
        if include_time:
            self.start_time = time.time()
        self.process = psutil.Process()
        self.startrss = self.process.memory_info().rss
        self.startvms = self.process.memory_info().vms

    def meminfo(self, message):
        if self.output_time:
            time_elapsed_seconds = time.time() - self.start_time
            print(f'{message:<30}:  time: {time_elapsed_seconds:8.2f} seconds    mem_percent {self.process.memory_percent()} ' +
                    f'delrss={self.process.memory_info().rss - self.startrss:16,}    ' +
                    f'delvms={self.process.memory_info().vms - self.startvms:16,}',
                    flush=True)
        else: # don't output time
            print(f'{message:<30}:  delrss={self.process.memory_info().rss - self.startrss:16,}   mem_percent {self.process.memory_percent()} ' +
                    f'delvms={self.process.memory_info().vms - self.startvms:16,}',
                    flush=True)

# mt = memtracker()


class GranuleCatalog:
    """
    Class to build ITS_LIVE granule catalog in geojson format for ingest by
    the webside DB.
    """
    FIVE_POINTS_PER_SIDE = True

    def __init__(self, granules_file: str, features_per_file: int, catalog_dir: str):
        """
        Initialize the object.
        """
        self.s3 = s3fs.S3FileSystem(anon=True)

        # read in granule file list from S3 file
        self.infiles = None
        logging.info(f"Opening granules file: {granules_file}")
        with self.s3.open(granules_file, 'r') as ins3file:
            self.infiles = json.load(ins3file)

        logging.info(f"Loaded {len(self.infiles)} granules from '{granules_file}'")

        self.features_per_file = features_per_file
        self.catalog_dir = catalog_dir

    def create(self, data_version, chunk_size, num_dask_workers, granules_dir):
        """
        Create catalog geojson file.
        """
        # read in granule file list from S3 file
        total_num_files = len(self.infiles)
        # mt.meminfo(f'Working on {total_num_files} total files')

        init_total_files = total_num_files

        if total_num_files <= 0:
            logging.info(f"Nothing to catalog, exiting.")
            return

        start = 0               # Current start index into global list
        read_num_files = 0      # Number of read files within the block
        block_start = 0         # Current start index for the block to write to file
        cum_read_num_files = 0  # Cumulative number of processed granules

        base_dir = os.path.basename(granules_dir)

        feature_list = []
        while total_num_files > 0:
            num_tasks = chunk_size if total_num_files > chunk_size else total_num_files

            logging.info(f"Starting granules {start}:{start+num_tasks} out of {init_total_files} total granules")
            tasks = [dask.delayed(self.image_pair_feature_from_path)(each, data_version, self.s3) for each in self.infiles[start:start+num_tasks]]
            results = None

            with ProgressBar():
                # Display progress bar
                results = dask.compute(tasks,
                                       scheduler="processes",
                                       num_workers=num_dask_workers)

            feature_list.extend(results[0])

            total_num_files -= num_tasks
            read_num_files += num_tasks
            cum_read_num_files += num_tasks
            start += num_tasks

            # Check if need to write to the file:
            if read_num_files >= self.features_per_file or total_num_files <= 0:
                # Use sub-directory name of input path as base for output filename
                featureColl = geojson.FeatureCollection(feature_list)
                outfilename = f'imgpair_{base_dir}_{block_start}_{cum_read_num_files-1}.json'

                with s3_out.open(f'{self.catalog_dir}/{outfilename}', 'w') as outf:
                    geojson.dump(featureColl, outf)

                # mt.meminfo(f'wrote {args.catalog_dir}/{outfilename}')
                logging.info(f'Wrote {self.catalog_dir}/{outfilename}')
                feature_list = []
                featureColl = None

                read_num_files = 0
                block_start = cum_read_num_files


    def image_pair_feature_from_path(self, infilewithpath: str, data_version: str, s3):
        filename_tokens = infilewithpath.split('/')
        directory = '/'.join(filename_tokens[1:-1])
        filename = filename_tokens[-1]

        if data_version is None:
            data_version = filename_tokens[-2]

        use_h5py = False
        with s3.open(f"s3://{infilewithpath}", "rb") as ins3:
            with xr.open_dataset(ins3, engine='h5netcdf') as inh5:
                if use_h5py is True:
                    # inh5 = h5py.File(ins3, mode = 'r')
                    # netCDF4/HDF5 cf 1.6 has x and y vectors of array pixel CENTERS
                    xvals = np.array(inh5.get('x'))
                    yvals = np.array(inh5.get('y'))

                    # Extract projection variable
                    projection_cf = None
                    if 'mapping' in inh5:
                        projection_cf = inh5['mapping']

                    elif 'UTM_Projection' in inh5:
                        projection_cf = inh5['UTM_Projection']

                    elif 'Polar_Stereographic' in inh5:
                        projection_cf = inh5['Polar_Stereographic']

                    imginfo_attrs = inh5['img_pair_info'].attrs
                    # turn hdf5 img_pair_info attrs into a python dict to save below
                    img_pair_info_dict = {}
                    for k in imginfo_attrs.keys():
                        if isinstance(imginfo_attrs[k], str):
                            img_pair_info_dict[k] = imginfo_attrs[k]

                        elif imginfo_attrs[k].shape == ():
                            img_pair_info_dict[k] = imginfo_attrs[k].decode('utf-8')  # h5py returns byte values, turn into byte characters

                        else:
                            img_pair_info_dict[k] = imginfo_attrs[k][0]    # h5py returns lists of numbers - all 1 element lists here, so dereference to number

                    num_pix_x = len(xvals)
                    num_pix_y = len(yvals)

                    minval_x, pix_size_x, rot_x_ignored, maxval_y, rot_y_ignored, pix_size_y = [float(x) for x in projection_cf.attrs['GeoTransform'].split()]

                    epsgcode = int(projection_cf.attrs['spatial_epsg'][0])
                    inh5.close()

                else:
                    # Use xarray interface to access granule's content
                    # inh5 = h5py.File(ins3, mode = 'r')
                    # netCDF4/HDF5 cf 1.6 has x and y vectors of array pixel CENTERS
                    xvals = inh5.x.values
                    yvals = inh5.y.values

                    # Extract projection variable
                    projection_cf = None
                    if 'mapping' in inh5:
                        projection_cf = inh5['mapping']

                    elif 'UTM_Projection' in inh5:
                        projection_cf = inh5['UTM_Projection']

                    elif 'Polar_Stereographic' in inh5:
                        projection_cf = inh5['Polar_Stereographic']

                    imginfo_attrs = inh5['img_pair_info'].attrs
                    # turn hdf5 img_pair_info attrs into a python dict to save below
                    img_pair_info_dict = {}
                    for k in imginfo_attrs.keys():
                        if isinstance(imginfo_attrs[k], str):
                            img_pair_info_dict[k] = imginfo_attrs[k]

                        elif imginfo_attrs[k].shape == ():
                            img_pair_info_dict[k] = imginfo_attrs[k].decode('utf-8')  # h5py returns byte values, turn into byte characters

                        else:
                            img_pair_info_dict[k] = imginfo_attrs[k][0]    # h5py returns lists of numbers - all 1 element lists here, so dereference to number

                    num_pix_x = len(xvals)
                    num_pix_y = len(yvals)

                    minval_x, pix_size_x, rot_x_ignored, maxval_y, rot_y_ignored, pix_size_y = [float(x) for x in projection_cf.attrs['GeoTransform'].split()]

                    epsgcode = int(projection_cf.attrs['spatial_epsg'][0])


        # NOTE: these are pixel center values, need to modify by half the grid size to get bounding box/geotransform values
        projection_cf_minx = xvals[0] - pix_size_x/2.0
        projection_cf_maxx = xvals[-1] + pix_size_x/2.0
        projection_cf_miny = yvals[-1] + pix_size_y/2.0 # pix_size_y is negative!
        projection_cf_maxy = yvals[0] - pix_size_y/2.0  # pix_size_y is negative!


        transformer = pyproj.Transformer.from_crs(f"EPSG:{epsgcode}", "EPSG:4326", always_xy=True) # ensure lonlat output order

        ll_lonlat = np.round(transformer.transform(projection_cf_minx,projection_cf_miny),decimals = 7).tolist()
        lr_lonlat = np.round(transformer.transform(projection_cf_maxx,projection_cf_miny),decimals = 7).tolist()
        ur_lonlat = np.round(transformer.transform(projection_cf_maxx,projection_cf_maxy),decimals = 7).tolist()
        ul_lonlat = np.round(transformer.transform(projection_cf_minx,projection_cf_maxy),decimals = 7).tolist()

        # find center lon lat for inclusion in feature (to determine lon lat grid cell directory)
    #     projection_cf_centerx = (xvals[0] + xvals[-1])/2.0
    #     projection_cf_centery = (yvals[0] + yvals[-1])/2.0
        center_lonlat = np.round(transformer.transform((xvals[0] + xvals[-1])/2.0,(yvals[0] + yvals[-1])/2.0 ),decimals = 7).tolist()

        if GranuleCatalog.FIVE_POINTS_PER_SIDE:
            fracs = [0.25, 0.5, 0.75]
            polylist = [] # ring in counterclockwise order

            polylist.append(ll_lonlat)
            dx = projection_cf_maxx - projection_cf_minx
            dy = projection_cf_miny - projection_cf_miny
            for frac in fracs:
                polylist.append(np.round(transformer.transform(projection_cf_minx + (frac * dx), projection_cf_miny + (frac * dy)),decimals = 7).tolist())

            polylist.append(lr_lonlat)
            dx = projection_cf_maxx - projection_cf_maxx
            dy = projection_cf_maxy - projection_cf_miny
            for frac in fracs:
                polylist.append(np.round(transformer.transform(projection_cf_maxx + (frac * dx), projection_cf_miny + (frac * dy)),decimals = 7).tolist())

            polylist.append(ur_lonlat)
            dx = projection_cf_minx - projection_cf_maxx
            dy = projection_cf_maxy - projection_cf_maxy
            for frac in fracs:
                polylist.append(np.round(transformer.transform(projection_cf_maxx + (frac * dx), projection_cf_maxy + (frac * dy)),decimals = 7).tolist())

            polylist.append(ul_lonlat)
            dx = projection_cf_minx - projection_cf_minx
            dy = projection_cf_miny - projection_cf_maxy
            for frac in fracs:
                polylist.append(np.round(transformer.transform(projection_cf_minx + (frac * dx), projection_cf_maxy + (frac * dy)),decimals = 7).tolist())

            polylist.append(ll_lonlat)

        else:
            # only the corner points
            polylist = [ ll_lonlat, lr_lonlat, ur_lonlat, ul_lonlat, ll_lonlat ]

        poly = geojson.Polygon([polylist])

        middate = img_pair_info_dict['date_center']
        deldays = img_pair_info_dict['date_dt']
        percent_valid_pix = img_pair_info_dict['roi_valid_percentage']

        feat = geojson.Feature( geometry=poly,
                                properties={
                                            'filename': filename,
                                            'directory': directory,
                                            'middate':middate,
                                            'deldays':deldays,
                                            'percent_valid_pix': percent_valid_pix,
                                            'center_lonlat':center_lonlat,
                                            'data_epsg':epsgcode,
                                            # date_deldays_strrep is a string version of center date and time interval that will sort by date and then by interval length (shorter intervals first) - relies on "string" comparisons by byte
                                            'date_deldays_strrep': img_pair_info_dict['date_center'] + f"{img_pair_info_dict['date_dt']:07.1f}".replace('.',''),
                                            'img_pair_info_dict': img_pair_info_dict,
                                            'version': data_version
                                            }
                                )
        return(feat)

if __name__ == '__main__':
    parser = argparse.ArgumentParser( \
        description="""make_geojson_features_for_imagepairs_v1.py

           produces output geojson FeatureCollection for each nn image_pairs from a directory.
           v1 adds 5 points per side to geom (so 3 interior and the two corners from v0)
           and the ability to stop the chunks (in addition to the start allowed in v0)
           so that the code can be run on a range of chunks.
        """,
        epilog="""
    There are two steps to create geojson catalogs:
    1. Create a list of granules to be used for catalog generation. The file that stores
       URLs of such granules is placed in the destination S3 bucket.
    2. Create geojson catalogs using a list of granules as generated by step #1. The list of
       granules is read from the file stored in the destination S3 bucket.""",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument('-granule_dir',
                        action='store',
                        type=str,
                        default='its-live-data.jpl.nasa.gov/velocity_image_pair/landsat/v02',
                        help='S3 path to tile catalog directories (not including the grid code for zone of tile) [%(default)s]')

    parser.add_argument('-catalog_dir',
                        action='store',
                        type=str,
                        default='its-live-data.jpl.nasa.gov/catalog_geojson/landsat/v02',
                        help='Output path for featurecollections [%(default)s]')

    parser.add_argument('-chunk_by',
                        action='store',
                        type=int,
                        default=1000,
                        help='Chunk feature collections to have chunk_by features each [%(default)d]')

    parser.add_argument('-features_per_file',
                        action='store',
                        type=int,
                        default=500000,
                        help='Number of features to store per the file [%(default)d]')

    parser.add_argument('-skipped_granules_file',
                        action='store',
                        type=str,
                        default='skipped_granules_landsat.json',
                        help='Filename to keep track of skipped duplicate granules [%(default)s], file is stored in "-granule_dir"')

    parser.add_argument('-catalog_granules_file',
                        action='store',
                        type=str,
                        default='used_granules_landsat.json',
                        help='Filename to keep track of granules used for the geojson catalog [%(default)s], file is stored in  "-granule_dir"')

    parser.add_argument('-c', '--create_catalog_list',
                        action='store_true',
                        help='build a list of granules for catalog generation [%(default)s], otherwise read the list of granules from catalog_granules_file')

    parser.add_argument('-glob',
                        action='store',
                        type=str,
                        default='*/*.nc',
                        help='Glob pattern for the granule search under "base_dir_s3fs" [%(default)s]')

    parser.add_argument('-five_points_per_side', action='store_true',
                        help='Define 5 points per side before re-projecting granule polygon to longitude/latitude coordinates')

    parser.add_argument('-data_version',
                        default=None,
                        type=str,
                        help='Data version to be recorded for each granule [%(default)s]. If none is provided, immediate parent directory of the granule is used as its version.')

    parser.add_argument('-w', '--dask_workers', type=int,
                        default=4,
                        help='Number of Dask parallel workers [%(default)d]')


    args = parser.parse_args()

    GranuleCatalog.FIVE_POINTS_PER_SIDE = args.five_points_per_side

    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',
                        datefmt='%m/%d/%Y %I:%M:%S %p', level=logging.INFO)

    logging.info(f'Command-line args: {args}')
    s3_out = s3fs.S3FileSystem()

    granules_dir = args.granule_dir

    if not args.create_catalog_list:
        catalog = GranuleCatalog(
            os.path.join(args.catalog_dir, args.catalog_granules_file),
            args.features_per_file,
            args.catalog_dir
        )
        catalog.create(
            args.data_version,
            args.chunk_by,
            args.dask_workers,
            granules_dir
        )

    else:
        # Create a list of granules to catalog and store it in S3 bucket
        # use a glob to list directory
        logging.info(f"Creating a list of granules to catalog")
        logging.info(f"Glob {granules_dir}/{args.glob}")
        infilelist = s3_out.glob(f'{granules_dir}/{args.glob}')
        logging.info(f"Got {len(infilelist)} granules")

        # check for '_P' in filename - filters out temp.nc files that can be left by bad transfers
        # also skips txt file placeholders for 000 Pct (all invalid) pairs
        infiles = [x for x in infilelist if '_P' in x and 'txt' not in x]

        # Skip duplicate granules (the same middle date, but different processing date)
        infiles, skipped_granules = skip_duplicate_granules(infiles)

        # Write all unique granules to the file
        granule_filename = os.path.join(args.catalog_dir, args.catalog_granules_file)
        with s3_out.open(granule_filename, 'w') as outf:
            geojson.dump(infiles, outf)

        logging.info(f"Wrote catalog granules to '{granule_filename}'")

        granule_filename = os.path.join(args.catalog_dir, args.skipped_granules_file)
        with s3_out.open(granule_filename, 'w') as outf:
            geojson.dump(skipped_granules, outf)

        logging.info(f"Wrote skipped granules to '{granule_filename}'")

    logging.info('Done.')
