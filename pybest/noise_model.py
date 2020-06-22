import os
import os.path as op
import numpy as np
import pandas as pd
import nibabel as nib
from tqdm import tqdm
from glob import glob
from nilearn import masking, signal, image
from joblib import Parallel, delayed
from sklearn.metrics import r2_score
from sklearn.linear_model import Ridge, LinearRegression
from sklearn.model_selection import RepeatedKFold, LeaveOneGroupOut

from .logging import tqdm_ctm, tdesc
from .utils import get_run_data, get_frame_times, create_design_matrix, hp_filter
from .utils import save_data, load_gifti, custom_clean
from .models import cross_val_r2

# IDEAS
# - "smarter" way to determine optimal n_comps (better than argmax)? regularize
# - use Kendrick's cutoff: 5% from optimal


def run_noise_processing(ddict, cfg, logger):
    """ Runs noise processing either within runs (i.e., separately for each run)
    when signalproc-type == 'single-trial' or across runs (i.e., on the run-concatenated data)
    using a cross-validated analysis when signalproc-type == 'glmdenoise'. """

    if cfg['skip_noiseproc']:
        logger.warn("Skipping noise processing (because of --skip-noiseproc)")
        ddict['denoised_func'] = ddict['preproc_func']
        n_runs = len(ddict['preproc_func'])
        ddict['opt_n_comps'] = np.zeros((n_runs, ddict['preproc_func'].shape[1]))
        save_data(ddict['opt_n_comps'], cfg, ddict, par_dir='denoising', run=None, desc='opt', dtype='ncomps')        
        return ddict

    logger.info(f"Starting denoising with {cfg['n_comps']} components")

    # Some parameters
    n_comps = np.arange(1, cfg['n_comps']+1).astype(int)  # range of components to test
    n_runs = np.unique(ddict['run_idx']).size
    K = ddict['preproc_func'].shape[1]  # voxels

    if cfg['signalproc_type'] == 'single-trial':
        # Denoising is done within runs!
        # Maybe add a "meta-seed" to cli options to ensure reproducibility?
        seed = np.random.randint(10e5)
        cv = RepeatedKFold(n_splits=cfg['cv_splits'], n_repeats=cfg['cv_repeats'], random_state=seed)

        # Parallel computation of R2 array (n_comps x voxels) across runs (list has len(runs))
        r2s_list = Parallel(n_jobs=cfg['n_cpus'])(delayed(_run_parallel_within_run)(
            run, ddict, cfg, logger, n_comps, cv) for run in range(n_runs)
        )

        ddict['opt_n_comps'] = np.zeros((n_runs, K))

        # Determine, per runs, the optimal number of noise comps
        for i, r2_ncomps in enumerate(r2s_list):
            # Compute maximum r2 across n-comps
            r2_max = r2_ncomps.max(axis=0)
            opt_n_comps_idx = r2_ncomps.argmax(axis=0)
            opt_n_comps = n_comps[opt_n_comps_idx.astype(int)]
            opt_n_comps[r2_max < 0] = 0  # set negative r2 voxels to 0 comps
        
            # Save for later denoising process (and signal-model, because we need
            # to orthogonalize our design matrix w.r.t. the confounds)
            ddict['opt_n_comps'][i, :] = opt_n_comps

            if cfg['save_all']:
                to_save = [(r2_ncomps, 'ncomps', 'r2'), (r2_ncomps.max(axis=0), 'max', 'r2'), (opt_n_comps, 'opt', 'ncomps')]
                for data, desc, dtype in to_save:
                    save_data(data, cfg, ddict, par_dir='denoising', run=None, desc=desc, dtype=dtype)

    else:  # Fit GLMdenoise-style models
        # Initialize R2 array (across HRFs/n-components/voxels)
        cv = LeaveOneGroupOut()
        r2s_list = Parallel(n_jobs=cfg['n_cpus'])(delayed(_run_parallel_across_runs)(
            ddict, cfg, logger, this_n_comp, cv) for this_n_comp
            in tqdm_ctm(n_comps, tdesc(f'Noise proc: '))
        )
        
        # r2: hrfs x n_components x voxels
        r2 = np.moveaxis(np.stack(r2s_list), [0, 1], [1, 0])
        
        # Best score across HRFs
        r2_ncomps = r2.max(axis=0)

        # Best overall r2 (across HRFs and n_comps)
        r2_max = r2_ncomps.max(axis=0)

        # Find optimal number of components and HRF index
        opt_n_comps = n_comps[r2_ncomps.argmax(axis=0).stype(int)]
        opt_n_comps[r2_max < 0] = 0
        opt_hrf_idx = np.zeros(K)

        for i in n_comps:
            idx = opt_n_comps == i
            opt_hrf_idx[idx] = r2[:, i-1, idx].argmax(axis=0)

        # Always save the following:
        save_data(opt_hrf_idx, cfg, ddict, par_dir='denoising', run=None, desc='opt', dtype='hrf')
        save_data(opt_n_comps, cfg, ddict, par_dir='denoising', run=None, desc='opt', dtype='ncomps')

        if cfg['save_all']:
            to_save = [(r2_ncomps, 'ncomps', 'r2'), (r2_ncomps.max(axis=0), 'max', 'r2')]
            for data, desc, dtype in to_save:
                save_data(data, cfg, ddict, par_dir='denoising', run=None, desc=desc, dtype=dtype)

        ddict['opt_hrf_idx'] = opt_hrf_idx
        ddict['opt_n_comps'] = np.tile(opt_n_comps, n_runs).reshape((n_runs, K))

    ### START DENOISING PROCESS ###

    # Pre-allocate clean func
    func_clean = ddict['preproc_func'].copy()
    for run in tqdm_ctm(range(n_runs), tdesc('Denoising funcs: ')):

        # Loop over unique indices
        func, conf, _ = get_run_data(ddict, run, func_type='preproc')
        nonzero = ~np.all(np.isclose(func, 0.), axis=0)
        for this_n_comps in np.unique(opt_n_comps).astype(int):
            # If n_comps is 0, then R2 was negative and we
            # don't want to denoise, so continue
            if this_n_comps == 0:
                continue

            # Find voxels that correspond to this_n_comps
            vox_idx = opt_n_comps == this_n_comps
            # Exclude voxels without signal
            vox_idx = np.logical_and(vox_idx, nonzero)

            C = conf[:, :this_n_comps]
            # Refit model on all data this time and remove fitted values
            func[:, vox_idx] = signal.clean(func[:, vox_idx], detrend=False, confounds=C, standardize=False)

        # Standardize once more
        func = signal.clean(func, detrend=False, standardize='zscore')
        func_clean[ddict['run_idx'] == run, :] = func

        # Save denoised data
        if cfg['save_all']:
            save_data(func, cfg, ddict, par_dir='denoising', run=run+1, desc='denoised',
                      dtype='bold', skip_if_single_run=True)

    # Always save full denoised timeseries (and optimal number of components for each run)
    save_data(func_clean, cfg, ddict, par_dir='denoising', run=None, desc='denoised', dtype='bold')
    save_data(ddict['opt_n_comps'], cfg, ddict, par_dir='denoising', run=None, desc='opt', dtype='ncomps')

    ddict['denoised_func'] = func_clean
    return ddict


def _run_parallel_within_run(run, ddict, cfg, logger, n_comps, cv):
    """ Function to evaluate noise model parallel across runs.
    Only used when signalproc-type == 'single-trial', because in case of
    'glmdenoise', the noise model is evaluated across runs
    """

    # Find indices of timepoints belong to this run
    func, conf, _ = get_run_data(ddict, run, func_type='preproc')
    nonzero = ~np.all(np.isclose(func, 0.), axis=0)
    
    # Pre-allocate R2-scores (components x voxels)
    r2s = np.zeros((n_comps.size, func.shape[1]))

    # Loop over number of components
    model = LinearRegression(fit_intercept=False, n_jobs=1)
    for i, n_comp in enumerate(tqdm_ctm(n_comps, tdesc(f'Noise proc run {run+1}:'))):
        # Check number of components
        if n_comp > conf.shape[1]:
            raise ValueError(f"Cannot select {n_comp} variables from conf data with {conf.shape[1]} components.")

        # Extract design matrix (with n_comp components)
        C = conf[:, :n_comp]
        r2s[i, nonzero] = cross_val_r2(model, C, func[:, nonzero], cv)

    return r2s


def _run_parallel_across_runs(ddict, cfg, logger, this_n_comp, cv):
    """ Run, per HRF, a cv model. """
    K = ddict['preproc_func'].shape[1]
    n_runs = np.unique(ddict['run_idx']).size

    if cfg['hrf_model'] == 'kay':
        r2 = np.zeros((20, K))
        to_iter = range(20)
    else:
        r2 = np.zeros((1, K))
        to_iter = range(1)

    # Define model (linreg) and cross-validation routine (leave-one-run-out)        
    model = LinearRegression(fit_intercept=False)
    # Define fMRI data (Y) and full confound matrix (C)
    Y = ddict['preproc_func'].copy()
    C = ddict['preproc_conf'].iloc[:, :this_n_comp].to_numpy()
    
    # Loop over HRFs
    for i in to_iter:
        Xs = []  # store runwise design matrix
        # Create run-wise design matrix
        for run in range(n_runs):
            t_idx = ddict['run_idx'] == run
            events = ddict['preproc_events'].query("run == (@run + 1)")
            tr = ddict['trs'][run]
            ft = get_frame_times(tr, ddict, cfg, Y[t_idx, :])
            X = create_design_matrix(tr, ft, events, hrf_model=cfg['hrf_model'], hrf_idx=i)
            X = X.iloc[:, :-1]  # remove intercept
            
            # Filter and remove confounds (C) from both the design matrix (X) and data (Y)
            X.loc[:, :], _ = custom_clean(X, Y, C, tr, ddict, cfg, clean_Y=False)
            X = X - X.mean(axis=0)
            Xs.append(X)

        # Concatenate across runs
        X = pd.concat(Xs, axis=0).to_numpy()
        Y = signal.clean(Y, detrend=False, standardize='zscore', confounds=C)

        # Cross-validation across runs
        r2[i, :] = cross_val_r2(model, X, Y, cv=cv, groups=ddict['run_idx'])

    return r2


def load_denoising_data(ddict, cfg):
    """ Loads the denoising parameters/data. """

    f_base = cfg['f_base']
    preproc_dir = op.join(cfg['save_dir'], 'preproc')
    denoising_dir = op.join(cfg['save_dir'], 'denoising')

    if 'fs' in cfg['space']:
        ddict['mask'] = None
        ddict['trs'] = [load_gifti(f)[1] for f in ddict['funcs']]
        ddict['opt_n_comps'] = np.load(op.join(denoising_dir, f'{f_base}_desc-opt_ncomps.npy'))
        ddict['opt_hrf_idx'] = np.load(op.join(denoising_dir, f'{f_base}_desc-opt_hrf.npy'))
        ddict['denoised_func'] = np.load(op.join(denoising_dir, f'{f_base}_desc-denoised_bold.npy'))
    else:
        ddict['mask'] = nib.load(op.join(preproc_dir, f'{f_base}_desc-preproc_mask.nii.gz'))
        ddict['trs'] = [nib.load(f).header['pixdim'][4] for f in ddict['funcs']]
        ddict['opt_n_comps'] = masking.apply_mask(op.join(denoising_dir, f'{f_base}_desc-opt_ncomps.nii.gz'), ddict['mask'])
        ddict['opt_hrf_idx'] = masking.apply_mask(op.join(denoising_dir, f'{f_base}_desc-opt_hrf.nii.gz'), ddict['mask'])
        ddict['denoised_func'] = masking.apply_mask(op.join(denoising_dir, f'{f_base}_desc-denoised_bold.nii.gz'), ddict['mask'])

    ddict['preproc_conf'] = pd.read_csv(op.join(preproc_dir, f'{f_base}_desc-preproc_conf.tsv'), sep='\t')

    if not cfg['skip_signalproc']:
        f_events = op.join(preproc_dir, f'{f_base}_desc-preproc_events.tsv')
        ddict['preproc_events'] = pd.read_csv(f_events, sep='\t')
    else:
        ddict['preproc_events'] = None
    
    ddict['run_idx'] = np.load(op.join(preproc_dir, 'run_idx.npy'))

    return ddict

