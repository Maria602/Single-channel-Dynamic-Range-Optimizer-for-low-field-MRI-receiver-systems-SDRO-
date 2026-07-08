"""Reconstruction module for noise measurement sequences."""

import marge.configs.hw_config as hw
import numpy as np
import scipy as sp


def SDRO(raw_data_path=None):
    """
    Required by MaRGE after sequenceRun.
    SDRO has no image output, so return empty results.
    """

    if raw_data_path is None:
        return None

    # load .mat
    mat_data = sp.io.loadmat(raw_data_path)
    output_dict = {}
    dicom_meta_data = {}
    outputs = []

    # Plot signal versus time
    result1 = {'widget': 'curve',
               'xData': np.array([0, 1]),
               'yData': [np.array([0, 0])],
               'xLabel': 'Useless',
               'yLabel': 'Useless',
               'title': 'Useless',
               'legend': ['Useless'],
               'row': 0,
               'col': 0}

    outputs.append(result1)

    return output_dict, outputs, dicom_meta_data