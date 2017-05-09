import os
import sys
import json
import time

import numpy as np
import pandas as pd
import dask

import histomicstk.preprocessing.color_normalization as htk_cnorm
import histomicstk.preprocessing.color_deconvolution as htk_cdeconv
import histomicstk.features as htk_features
import histomicstk.utils as htk_utils

import large_image

from ctk_cli import CLIArgumentParser

import logging
logging.basicConfig(level=logging.CRITICAL)

sys.path.append(os.path.normpath(os.path.join(os.path.dirname(__file__), '..')))
from cli_common import utils as cli_utils # noqa


def compute_tile_nuclei_features(slide_path, tile_position, args, **it_kwargs):

    # get slide tile source
    ts = large_image.getTileSource(slide_path)

    # get requested tile
    tile_info = ts.getSingleTile(
        tile_position=tile_position,
        format=large_image.tilesource.TILE_FORMAT_NUMPY,
        **it_kwargs)

    # get tile image
    im_tile = tile_info['tile'][:, :, :3]

    # perform color normalization
    im_nmzd = htk_cnorm.reinhard(im_tile,
                                 args.reference_mu_lab,
                                 args.reference_std_lab)

    # perform color decovolution
    w = cli_utils.get_stain_matrix(args)

    im_stains = htk_cdeconv.color_deconvolution(im_nmzd, w).Stains

    im_nuclei_stain = im_stains[:, :, 0].astype(np.float)

    # segment nuclei
    im_nuclei_seg_mask = cli_utils.detect_nuclei_kofahi(im_nuclei_stain, args)

    # generate nuclei annotations
    nuclei_annot_list = cli_utils.create_tile_nuclei_annotations(
        im_nuclei_seg_mask, tile_info, args.nuclei_annotation_format)

    # compute nuclei features
    if args.cytoplasm_features:
        im_cytoplasm_stain = im_stains[:, :, 1].astype(np.float)
    else:
        im_cytoplasm_stain = None

    fdata = htk_features.compute_nuclei_features(
        im_nuclei_seg_mask, im_nuclei_stain, im_cytoplasm_stain,
        fsd_bnd_pts=args.fsd_bnd_pts,
        fsd_freq_bins=args.fsd_freq_bins,
        cyto_width=args.cyto_width,
        num_glcm_levels=args.num_glcm_levels,
        morphometry_features_flag=args.morphometry_features,
        fsd_features_flag=args.fsd_features,
        intensity_features_flag=args.intensity_features,
        gradient_features_flag=args.gradient_features,
    )

    fdata.columns = ['Feature.' + col for col in fdata.columns]

    return nuclei_annot_list, fdata


def disp_time(seconds):
    return time.strftime("%H:%M:%S", time.gmtime(seconds))


def main(args):

    total_start_time = time.time()

    print('\n>> CLI Parameters ...\n')

    print args

    if not os.path.isfile(args.inputImageFile):
        raise IOError('Input image file does not exist.')

    if len(args.reference_mu_lab) != 3:
        raise ValueError('Reference Mean LAB should be a 3 element vector.')

    if len(args.reference_std_lab) != 3:
        raise ValueError('Reference Stddev LAB should be a 3 element vector.')

    if len(args.analysis_roi) != 4:
        raise ValueError('Analysis ROI must be a vector of 4 elements.')

    if np.all(np.array(args.analysis_roi) == -1):
        process_whole_image = True
    else:
        process_whole_image = False

    #
    # Initiate Dask client
    #
    print('\n>> Creating Dask client ...\n')

    c = cli_utils.create_dask_client(args)

    print c

    #
    # Read Input Image
    #
    print('\n>> Reading input image ... \n')

    ts = large_image.getTileSource(args.inputImageFile)

    ts_metadata = ts.getMetadata()

    print json.dumps(ts_metadata, indent=2)

    is_wsi = ts_metadata['magnification'] is not None

    #
    # Compute tissue/foreground mask at low-res for whole slide images
    #
    if is_wsi:

        print('\n>> Computing tissue/foreground mask at low-res ...\n')

        im_fgnd_mask_lres, fgnd_seg_scale = \
            cli_utils.segment_wsi_foreground_at_low_res(ts)

    #
    # Compute foreground fraction of tiles in parallel using Dask
    #
    tile_fgnd_frac_list = [1.0]

    it_kwargs = {
        'tile_size': {'width': args.analysis_tile_size},
        'scale': {'magnification': args.analysis_mag},
    }

    if not process_whole_image:

        it_kwargs['region'] = {
            'left':   args.analysis_roi[0],
            'top':    args.analysis_roi[1],
            'width':  args.analysis_roi[2],
            'height': args.analysis_roi[3],
            'units':  'base_pixels'
        }

    if is_wsi:

        print('\n>> Computing foreground fraction of all tiles ...\n')

        start_time = time.time()

        num_tiles = ts.getSingleTile(**it_kwargs)['iterator_range']['position']

        print 'Number of tiles = %d' % num_tiles

        tile_fgnd_frac_list = htk_utils.compute_tile_foreground_fraction(
            args.inputImageFile, im_fgnd_mask_lres, fgnd_seg_scale,
            **it_kwargs
        )

        num_fgnd_tiles = np.count_nonzero(
            tile_fgnd_frac_list >= args.min_fgnd_frac)

        percent_fgnd_tiles = 100.0 * num_fgnd_tiles / num_tiles

        fgnd_frac_comp_time = time.time() - start_time

        print 'Number of foreground tiles = %d (%.2f%%)' % (
            num_fgnd_tiles, percent_fgnd_tiles)

        print 'Time taken = %s' % disp_time(fgnd_frac_comp_time)

    #
    # Detect and compute nuclei features in parallel using Dask
    #
    print('\n>> Detecting nuclei and computing features ...\n')

    start_time = time.time()

    tile_result_list = []

    for tile in ts.tileIterator(**it_kwargs):

        tile_position = tile['tile_position']['position']

        if is_wsi and tile_fgnd_frac_list[tile_position] <= args.min_fgnd_frac:
            continue

        # detect nuclei
        cur_result = dask.delayed(compute_tile_nuclei_features)(
            args.inputImageFile,
            tile_position,
            args, **it_kwargs)

        # append result to list
        tile_result_list.append(cur_result)

    tile_result_list = dask.delayed(tile_result_list).compute()

    nuclei_annot_list = [annot
                         for annot_list, fdata in tile_result_list
                         for annot in annot_list]

    nuclei_fdata = pd.concat([fdata for annot_list, fdata in tile_result_list],
                             ignore_index=True)

    nuclei_detection_time = time.time() - start_time

    print 'Number of nuclei = ', len(nuclei_annot_list)
    print "Time taken = %s" % disp_time(nuclei_detection_time)

    #
    # Write annotation file
    #
    print('\n>> Writing annotation file ...\n')

    if args.outputNucleiAnnotationFile:

        annot_fname = os.path.splitext(
            os.path.basename(args.outputNucleiAnnotationFile))[0]

        annotation = {
            "name": annot_fname + '-nuclei-' + args.nuclei_annotation_format,
            "elements": nuclei_annot_list
        }

        with open(args.outputNucleiAnnotationFile, 'w') as annotation_file:
            json.dump(annotation, annotation_file, indent=2, sort_keys=False)

    #
    # Create CSV Feature file
    #
    print('>> Writing CSV feature file')

    nuclei_fdata.to_csv(args.outputFile)

    total_time_taken = time.time() - total_start_time

    print 'Total analysis time = %s' % disp_time(total_time_taken)


if __name__ == "__main__":
    main(CLIArgumentParser().parse_args())
