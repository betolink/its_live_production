import json
import requests
import pyproj
import numpy as np
import os
import logging
import time
import sys
import zipfile

from grid import Bounds

BASE_URL = 'https://nsidc.org/apps/itslive-search/velocities/urls'
# BASE_URL = 'https://staging.nsidc.org/apps/itslive-search/velocities/urls'


def transform_coord(proj1, proj2, lon, lat):
    """Transform coordinates from proj1 to proj2 (EPSG num)."""
    # Set full EPSG projection strings
    proj1 = pyproj.Proj("+init=EPSG:"+proj1)
    proj2 = pyproj.Proj("+init=EPSG:"+proj2)
    # Convert coordinates
    return pyproj.transform(proj1, proj2, lon, lat)


def get_granule_urls(params):
    # Allow for longer query time from searchAPI: 10 minutes
    resp = requests.get(BASE_URL, params=params, verify=False, timeout=500)
    return resp.json()


def get_granule_urls_compressed(params, total_retries=1, num_seconds=30):
    """
    Request granules URLs with ZIP compression enabled, save the stream to the ZIP file,
    and retrieve JSON information from the archive.

    params: request parameters
    total_retries: number of retries to query searchAPI in a case of exception.
                    Default is 1.
    num_seconds: number of seconds to sleep between retries to query searchAPI.
                    Default is 30 seconds.
    """
    # Format request URL:
    url = f'{BASE_URL}?'
    for each_key, each_value in params.items():
        url += f'{each_key}={each_value}&'

    # Add compression option
    url += 'compressed=true&'

    # Add requested granules version (TODO: should be configurable on startup?)
    url += 'version=2'

    # Get rid of all single quotes if any in URL
    url = url.replace("'", "")

    num_retries = 0
    got_granules = False
    data = []

    # Save response to local file:
    local_path = 'searchAPI_urls.json.zip'

    logging.info(f'Submitting searchAPI request with url={url}')

    while not got_granules and num_retries < total_retries:
        # Get list of granules:
        try:
            logging.info(f"Getting granules from searchAPI: #{num_retries+1} attempt")
            num_retries += 1

            resp = requests.get(url, stream=True, timeout=500)

            logging.info(f'Saving searchAPI response to {local_path}')
            with open(local_path, 'wb') as fh:
                for chunk in resp.iter_content(10240, decode_unicode=False):
                    _ = fh.write(chunk)

            # Unzip the file
            with zipfile.ZipFile(local_path, 'r') as fh:
                zip_json_file = fh.namelist()[0]
                logging.info(f'Extracting {zip_json_file}')

                with fh.open(zip_json_file) as fh_json:
                    data = json.load(fh_json)

                    got_granules = True

        except:
            # If failed due to response truncation or searchAPI not being able to respond
            # (too many requests at the same time?)
            logging.info(f'Got exception: {sys.exc_info()}')
            if num_retries < total_retries:
                # Sleep if it's not last attempt
                logging.info(f'Sleeping between searchAPI attempts for {num_seconds} seconds...')
                time.sleep(num_seconds)

        finally:
            # Clean up local file
            if os.path.exists(local_path):
                # Remove local file
                logging.info(f'Removing {local_path}')
                os.unlink(local_path)

    if not got_granules:
        raise RuntimeError("Failed to get granules from searchAPI.")

    return data


def get_granule_urls_streamed(params, total_retries=1, num_seconds=30):
    """
    Use streamed retrieval of the response from URL request.

    params: request parameters
    total_retries: number of retries to query searchAPI in a case of exception.
                    Default is 1.
    num_seconds: number of seconds to sleep between retries to query searchAPI.
                    Default is 30 seconds.
    """
    token = ']['

    # Format request URL:
    url = f'{BASE_URL}?'
    for each_key, each_value in params.items():
        url += f'{each_key}={each_value}&'

    # Add requested granules version (TODO: should be configurable on startup?)
    url += 'version=2'

    # Get rid of all single quotes if any in URL
    url = url.replace("'", "")

    num_retries = 0
    got_granules = False
    data = []

    # Save response to local file:
    local_path = 'searchAPI_urls.json'

    logging.info(f'Submitting searchAPI request with url={url}')

    while not got_granules and num_retries < total_retries:
        # Get list of granules:
        try:
            logging.info(f"Getting granules from searchAPI: #{num_retries+1} attempt")
            num_retries += 1

            resp = requests.get(url, stream=True, timeout=500)

            logging.info(f'Saving searchAPI response to {local_path}')
            with open(local_path, 'a') as fh:
                for chunk in resp.iter_content(10240, decode_unicode=True):
                    _ = fh.write(chunk)

            # Read data from local file
            data = ''
            with open(local_path) as fh:
                data = fh.readline()

            # if multiple json strings are returned,  then possible to see '][' within
            # the string, replace it by ','
            if token in data:
                logging.info('Got multiple json variables within the same string (len(data)={len(data)})')
                data = data.replace(token, ',')

                logging.info('Merged multiple json variables into one list (len(data)={len(data)})')

            data = json.loads(data)
            got_granules = True

        except:
            # If failed due to response truncation or searchAPI not being able to respond
            # (too many requests at the same time?)
            logging.info(f'Got exception: {sys.exc_info()}')
            if num_retries < total_retries:
                # Sleep if it's not last attempt
                logging.info(f'Sleeping between searchAPI attempts for {num_seconds} seconds...')
                time.sleep(num_seconds)

        finally:
            # Clean up local file
            if os.path.exists(local_path):
                # Remove local file
                logging.info(f'Removing {local_path}')
                os.unlink(local_path)

    if not got_granules:
        raise RuntimeError("Failed to get granules from searchAPI.")

    return data


#
# Author: Mark Fahnestock
#
def point_to_prefix(lat: float, lon: float, dir_path: str = None) -> str:
    """
    Returns a string (for example, N78W124) for directory name based on
    granule centerpoint lat,lon
    """
    NShemi_str = 'N' if lat >= 0.0 else 'S'
    EWhemi_str = 'E' if lon >= 0.0 else 'W'

    outlat = int(10*np.trunc(np.abs(lat/10.0)))
    if outlat == 90:  # if you are exactly at a pole, put in lat = 80 bin
        outlat = 80

    outlon = int(10*np.trunc(np.abs(lon/10.0)))

    if outlon >= 180:  # if you are at the dateline, back off to the 170 bin
        outlon = 170

    dirstring = f'{NShemi_str}{outlat:02d}{EWhemi_str}{outlon:03d}'
    if dir_path is not None:
        dirstring = os.path.join(dir_path, dirstring)

    return dirstring


#
# Author: Mark Fahnestock, Masha Liukis
#
def add_five_points_to_polygon_side(polygon):
    """
    Define 5 points per each polygon side. This is done before re-projecting
    polygon to longitude/latitude coordinates.
    This function assumes rectangular polygon where min/max x/y define all
    4 polygon vertices.

    polygon: list of lists
        List of polygon vertices.
    """
    fracs = [0.25, 0.5, 0.75]
    polylist = []  # closed ring of polygon points

    # Determine min/max x/y values for the polygon
    x = Bounds([each[0] for each in polygon])
    y = Bounds([each[1] for each in polygon])

    polylist.append((x.min, y.min))
    dx = x.max - x.min
    dy = y.min - y.min
    for frac in fracs:
        polylist.append((x.min + frac * dx, y.min + frac * dy))

    polylist.append((x.max, y.min))
    dx = x.max - x.max
    dy = y.max - y.min
    for frac in fracs:
        polylist.append((x.max + frac * dx, y.min + frac * dy))

    polylist.append((x.max, y.max))
    dx = x.min - x.max
    dy = y.max - y.max
    for frac in fracs:
        polylist.append((x.max + frac * dx, y.max + frac * dy))

    polylist.append((x.min, y.max))
    dx = x.min - x.min
    dy = y.min - y.max
    for frac in fracs:
        polylist.append((x.min + frac * dx, y.max + frac * dy))

    polylist.append((x.min, y.min))

    return polylist
