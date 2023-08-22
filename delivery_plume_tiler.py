import argparse
import subprocess
import datetime

import masked_plume_delineator
import logging
from spectral.io import envi
import numpy as np
import os
from utils import envi_header
from osgeo import gdal
import pandas as pd
import time
import json
import glob
from scrape_refine_upload import write_color_plume
from apply_glt import single_image_ortho
from copy import deepcopy
from scrape_refine_upload_ms import rawspace_coordinate_conversion
from rasterio.features import rasterize
from shapely.geometry import Polygon
import matplotlib.pyplot as plt

class SerialEncoder(json.JSONEncoder):
    """Encoder for json to help ensure json objects can be passed to the workflow manager.
    """

    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        else:
            return super(SerialEncoder, self).default(obj)

def write_science_cog(output_img, output_file, geotransform, projection, metadata):
    tmp_file = os.path.splitext(output_file)[0] + '_tmp.tif'
    driver = gdal.GetDriverByName('GTiff')
    driver.Register()
    outDataset = driver.Create(tmp_file,output_img.shape[1],output_img.shape[0],1,gdal.GDT_Float32,options = ['COMPRESS=LZW'])
    md = outDataset.GetMetadata()
    md.update(metadata)
    outDataset.SetMetadata(md)
    outDataset.GetRasterBand(1).WriteArray(output_img)
    outDataset.GetRasterBand(1).SetNoDataValue(-9999)
    outDataset.SetProjection(projection)
    outDataset.SetGeoTransform(geotransform)
    del outDataset

    subprocess.call(f'sh /home/brodrick/bin/cog.sh {tmp_file} {output_file}',shell=True)
    subprocess.call(f'rm {tmp_file}',shell=True)
    

def single_plume_proc(all_plume_meta, index, output_base, dcid_sourcedir, source_dir, extra_metadata):

        plume_dict = {"crs": {"properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84" }, "type": "name"},"features":[],"name":"methane_metadata","type":"FeatureCollection" }
        plume_dict['features'] = [deepcopy(all_plume_meta['features'][index])]
        plume_id = plume_dict['features'][0]['properties']['Plume ID']

        plume_output_file = os.path.join(output_base + '.json')
        with open(plume_output_file, 'w') as fout:
            fout.write(json.dumps(plume_dict, cls=SerialEncoder)) 

        # rasterize that polygon
        ds = gdal.Open(os.path.join(dcid_sourcedir, f'dcid_{plume_dict["features"][0]["properties"]["DCID"]}_mf_ort.tif'))
        dat = ds.ReadAsArray().squeeze()

        print(plume_dict['features'][0]['geometry']['coordinates'][0])
        rawspace_coords = rawspace_coordinate_conversion([], plume_dict['features'][0]['geometry']['coordinates'][0], ds.GetGeoTransform(), ortho=True)
        manual_mask = rasterize(shapes=[Polygon(rawspace_coords)], out_shape=(dat.shape[0],dat.shape[1])) # numpy binary mask for manual IDs

        y_locs = np.where(np.sum(manual_mask > 0, axis=1))[0]
        x_locs = np.where(np.sum(manual_mask > 0, axis=0))[0]

        dat[manual_mask < 1] = -9999
        dat = dat[y_locs[0]:y_locs[-1],x_locs[0]:x_locs[-1]]
        outtrans = list(ds.GetGeoTransform())
        outtrans[0] = outtrans[0] + x_locs[0]*outtrans[1]
        outtrans[3] = outtrans[3] + y_locs[0]*outtrans[5]

        scene_names = []
        for _s in range(len(plume_dict['features'][0]['properties']['Scene FIDs'])):
            fid =plume_dict['features'][0]['properties']['Scene FIDs'][_s]
            scene =plume_dict['features'][0]['properties']['DAAC Scene Numbers'][_s]
            orbit =plume_dict['features'][0]['properties']['Orbit']
            scene_names.append(f'EMIT_L2B_CH4ENH_{extra_metadata["product_version"]}_{fid[4:12]}T{fid[13:19]}_{orbit}_{scene}')

        metadata = {
            'Plume_Complex': plume_dict['features'][0]['properties']['Plume ID'],
            'Orbit': plume_dict['features'][0]['properties']['Orbit'],
            'dcid': plume_dict['features'][0]['properties']['DCID'],
            'Estimated_Uncertainty_ppmm': plume_dict['features'][0]['properties']['Concentration Uncertainty (ppm m)'],
            'UTC_Time_Observed': plume_dict['features'][0]['properties']['UTC Time Observed'],
            #Source_Scenes - match full conventions j
            'Source_Scenes': ','.join(scene_names),
            'Units': 'ppm m',
            'Latitude of max concentration': plume_dict['features'][0]['properties']['Latitude of max concentration'],
            'Longitude of max concentration': plume_dict['features'][0]['properties']['Longitude of max concentration'],
            'Max Plume Concentration (ppm m)': plume_dict['features'][0]['properties']['Max Plume Concentration (ppm m)'],
            }
        metadata.update(extra_metadata)
        write_science_cog(dat, output_base + '.tif', outtrans, ds.GetProjection(), metadata)
        write_color_quicklook(dat, output_base + '.png')


def write_color_quicklook(indat, output_file):

    dat = indat.copy()
    mask = dat != -9999
    dat[dat < 0] = 0
    dat = dat /1500.
    output = np.zeros((indat.shape[0],indat.shape[1],3),dtype=np.uint8)
    output[mask,:] = np.round(plt.cm.plasma(dat[mask])[...,:3] * 255).astype(np.uint8)
    output[mask,:] = np.maximum(1, output[mask])


    memdriver = gdal.GetDriverByName('MEM')
    memdriver.Register()
    outDataset = memdriver.Create('',dat.shape[1],dat.shape[0],3,gdal.GDT_Byte)
    for n in range(1,4):
        outDataset.GetRasterBand(n).WriteArray(output[...,n-1])
        outDataset.GetRasterBand(n).SetNoDataValue(0)

    driver = gdal.GetDriverByName('PNG')
    driver.Register()
    dst_ds = driver.CreateCopy(output_file, outDataset, strict=0)
    del dst_ds, outDataset
    

def single_scene_proc(input_file, output_file, extra_metadata):
    ds = gdal.Open(input_file)
    dat = ds.ReadAsArray().squeeze()
    write_science_cog(dat, output_file, ds.GetGeoTransform(), ds.GetProjection(), extra_metadata)
    write_color_quicklook(dat, output_file.replace('.tif','.png'))






def main(input_args=None):
    parser = argparse.ArgumentParser(description="Delineate/colorize plume")
    parser.add_argument('--source_dir', type=str, default='methane_20230813')
    parser.add_argument('--dest_dir', type=str, default='visions_delivery')
    parser.add_argument('--manual_del_dir', type=str, default='/beegfs/scratch/brodrick/methane/ch4_plumedir_scenetest/')
    parser.add_argument('--software_version', type=str, default=None)
    parser.add_argument('--data_version', type=str, default=None)
    args = parser.parse_args(input_args)

    tile_dir = os.path.join(args.dest_dir, 'ch4_plume_tiles')


    all_plume_meta = json.load(open(f'{args.manual_del_dir}/combined_plume_metadata.json'))
    unique_fids = np.unique([sublist for feat in all_plume_meta['features'] for sublist in feat['properties']['Scene FIDs']])

    plume_count = 1

    for _feat, feat in enumerate(all_plume_meta['features']):
        print(f'{_feat+1}/{len(all_plume_meta["features"])}')

        extra_metadata = {}
        if args.software_version:
            extra_metadata['software_build_version'] = args.software_version
        if args.data_version:
            extra_metadata['product_version'] = args.data_version
        extra_metadata['keywords'] = "Imaging Spectroscopy, minerals, EMIT, dust, radiative forcing"
        extra_metadata['sensor'] = "EMIT (Earth Surface Mineral Dust Source Investigation)"
        extra_metadata['instrument'] = "EMIT"
        extra_metadata['platform'] = "ISS"
        extra_metadata['Conventions'] = "CF-1.63"
        extra_metadata['institution'] = "NASA Jet Propulsion Laboratory/California Institute of Technology"
        extra_metadata['license'] = "https://science.nasa.gov/earth-science/earth-science-data/data-information-policy/"
        extra_metadata['naming_authority'] = "LPDAAC"
        extra_metadata['date_created'] = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        extra_metadata['keywords_vocabulary'] = "NASA Global Change Master Directory (GCMD) Science Keywords"
        extra_metadata['stdname_vocabulary'] = "NetCDF Climate and Forecast (CF) Metadata Convention"
        extra_metadata['creator_name'] = "Jet Propulsion Laboratory/California Institute of Technology"
        extra_metadata['creator_url'] = "https://earth.jpl.nasa.gov/emit/"
        extra_metadata['project'] = "Earth Surface Mineral Dust Source Investigation"
        extra_metadata['project_url'] = "https://earth.jpl.nasa.gov/emit/"
        extra_metadata['publisher_name'] = "NASA LPDAAC"
        extra_metadata['publisher_url'] = "https://lpdaac.usgs.gov"
        extra_metadata['publisher_email'] = "lpdaac@usgs.gov"
        extra_metadata['identifier_product_doi_authority'] = "https://doi.org"
        extra_metadata['title'] = "EMIT"

        if feat['geometry']['type'] == 'Polygon':
            outdir=os.path.join(args.dest_dir, feat['properties']['Scene FIDs'][0][4:12], 'l2bch4plm')
            if os.path.isdir(outdir) is False:
                subprocess.call(f'mkdir -p {outdir}',shell=True)
            single_plume_proc(all_plume_meta, _feat, os.path.join(outdir, feat['properties']['Scene FIDs'][0] + '_' + feat['properties']['Plume ID']), args.manual_del_dir, args.source_dir, extra_metadata)



    for fid in unique_fids:
        outdir = os.path.join(args.dest_dir, fid[4:12], 'l2bch4enh')
        if os.path.isdir(outdir) is False:
            subprocess.call(f'mkdir -p {outdir}',shell=True)
        single_scene_proc(os.path.join(args.source_dir, fid[4:12], fid + '_ch4_mf_ort'),  os.path.join(outdir, fid + 'ch4_enh.tif'), extra_metadata)

    # make PNGs for both









if __name__ == '__main__':
    main()


