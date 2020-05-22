import io
import os
import click
import os.path as op
import numpy as np
import nibabel as nib
from tqdm import tqdm
from glob import glob
from nilearn import plotting
from nilearn.datasets import fetch_surf_fsaverage


def check_parameters(cfg, logger):
    """ Checks parameter settings and raises errors in case of
    incompatible parameters. """
    
    if 'fs' in cfg['space'] and cfg['tr'] is None:
        raise ValueError("TR (--tr) needs to be set when using surface data (--space fs*)!")

    if cfg['single_trial_id'] is None:
        logger.warn(f"Empty single-trial-id; all events will be modeled as single trials!")
        cfg['single_trial_id'] = ''

    if cfg['uncorrelation'] and cfg['single_trial_model'] == 'lss':
        raise ValueError("Cannot use uncorrelation in combination with LSS.")


def set_defaults(cfg, logger):
    """ Sets default inputs. """
    if not op.isdir(cfg['bids_dir']):
        raise ValueError(f"BIDS directory {cfg['bids_dir']} does not exist!")

    logger.info(f"Using BIDS directory {cfg['bids_dir']}")

    if cfg['out_dir'] is None:  # Set default out_dir
        cfg['out_dir'] = op.join(cfg['bids_dir'], 'derivatives', 'pybest')
        if not op.isdir(cfg['out_dir']):
            logger.info(f"Creating output-directory {cfg['out_dir']}")
            os.makedirs(cfg['out_dir'], exist_ok=True)

        logger.info(f"Setting output directory to {cfg['out_dir']}")

    if cfg['fprep_dir'] is None:
        cfg['fprep_dir'] = op.join(cfg['bids_dir'], 'derivatives', 'fmriprep')
        if not op.isdir(cfg['fprep_dir']):
            raise ValueError(f"Fmriprep directory {cfg['fprep_dir']} does not exist.")

        logger.info(f"Setting Fmriprep directory to {cfg['fprep_dir']}")

    if cfg['ricor_dir'] is None:
        cfg['ricor_dir'] = op.join(cfg['bids_dir'], 'derivatives', 'physiology')
        if not op.isdir(cfg['ricor_dir']):
            cfg['ricor_dir'] = None
            logger.info("No RETROICOR directory, so assuming no physio data.")
    
    if cfg['ricor_dir'] is not None:
        logger.info(f"Setting RETROICOR directory to {cfg['ricor_dir']}")

    if cfg['gm_thresh'] == 0:
        cfg['gm_thresh'] = None

    if not cfg['subject']:
        cfg['subject'] = None

    return cfg


def find_exp_parameters(cfg, logger):
    """ Extracts experimental parameters. """
    # Use all possible participants if not provided
    if cfg['subject'] is None:
        cfg['subject'] = [
            op.basename(s).split('-')[1] for s in
            sorted(glob(op.join(cfg['fprep_dir'], 'sub-*')))
            if op.isdir(s)
        ]
        logger.info(f"Found {len(cfg['subject'])} participant(s)")
    else:
        # Use a list by default
        cfg['subject'] = [cfg['subject']]

    # Use all sessions if not provided
    if cfg['session'] is None:
        cfg['session'] = []
        for this_sub in cfg['subject']:
            these_ses = [
                op.basename(s).split('-')[1] for s in
                sorted(glob(op.join(cfg['fprep_dir'], f'sub-{this_sub}', 'ses-*')))
                if op.isdir(s)
            ]
            cfg['session'].append(these_ses)
            logger.info(f"Found {len(these_ses)} session(s) for sub-{this_sub}")
    else:
        cfg['session'] = [cfg['session']] * len(cfg['subject'])

    # Use all tasks if no explicit task is provided
    if cfg['task'] is None:
        cfg['task'] = []
        for this_sub, these_ses in zip(cfg['subject'], cfg['session']):
            these_task = []
            for this_ses in these_ses:
                
                tmp = glob(op.join(
                    cfg['bids_dir'],
                    f'sub-{this_sub}',
                    f'ses-{this_ses}',
                    'func',
                    f'*_events.tsv'
                ))

                these_ses_task = list(set(
                    [op.basename(f).split('task-')[1].split('_')[0] for f in tmp]
                ))
        
                these_task.append(these_ses_task)
                logger.info(f"Found {len(these_ses_task)} task(s) for sub-{this_sub} and ses-{this_ses}")

            cfg['task'].append(these_task)
    else:
        cfg['task'] = [[cfg['task']] * len(cfg['session'])] * len(cfg['subject'])

    return cfg


def find_data(cfg, logger):
    """ Finds all data for a given subject/session/task/space/hemi. """
    # Set right "identifier" depending on fsaverage* or volumetric space
    sub, ses, task, hemi, space = cfg['sub'], cfg['ses'], cfg['task'], cfg['hemi'], cfg['space']
    space_idf = f'hemi-{hemi}.func.gii' if 'fs' in space else 'desc-preproc_bold.nii.gz'

    # Gather funcs, confs, tasks
    fprep_dir = cfg['fprep_dir']
    funcs = sorted(glob(op.join(
        fprep_dir, f'sub-{sub}', f'ses-{ses}', 'func', f'*task-{task}_*_space-{space}_{space_idf}'
    )))
    confs = sorted(glob(op.join(
        fprep_dir, f'sub-{sub}', f'ses-{ses}', 'func', f'*desc-confounds_regressors.tsv'
    )))
    bids_dir = cfg['bids_dir']
    events = sorted(glob(op.join(
        bids_dir, f'sub-{sub}', f'ses-{ses}', 'func', f'*task-{task}_*_events.tsv'
    )))

    # Check if complete
    if not all(len(funcs) == len(tmp) for tmp in [confs, events]):
        raise ValueError(
            f"Found unequal number of funcs ({len(funcs)}), confs ({len(confs)}), and events ({len(events)})."
        )
    logger.info(f"Found {len(funcs)} runs for task {task}")

    # Also find retroicor files
    ricor_dir = cfg['ricor_dir']
    if ricor_dir is not None:
        ricors = sorted(glob(op.join(
            ricor_dir, f'sub-{sub}', f'ses-{ses}', 'physio', f'*task-{task}_*_regressors.tsv'
        )))
        logger.info(f"Found {len(ricors)} RETROICOR files for task {task}")
    else:
        ricors = None

    if 'fs' not in space and cfg['gm_thresh'] is not None:  # volumetric files
        space_idf = '' if space == 'T1w' else f'_space-{space}'
        fname = f'sub-{sub}{space_idf}_label-GM_probseg.nii.gz'
        gm_prob = op.join(fprep_dir, f'sub-{sub}', 'anat', fname)
    else:
        gm_prob = None

    ddict = dict(
        funcs=funcs, confs=confs, events=events,
        ricors=ricors, gm_prob=gm_prob
    )

    if cfg['tr'] is None:
        tr = np.round(nib.load(funcs[0]).header['pixdim'][4], 3)
        logger.warning(f"TR is not set; using TR from first func ({tr:.3f} sec.)")

    # Store TR in data dict (maybe should use cfg?)
    ddict['tr'] = tr

    return ddict


def _load_gifti(f):
    """ Load gifti array. """
    f_gif = nib.load(f)
    return np.vstack([arr.data for arr in f_gif.darrays])


@click.command()
@click.argument('file')
@click.option('--hemi', default='L', type=click.Choice(['L', 'R']), required=False)
@click.option('--space', default='fsaverage6', type=click.Choice(['fsaverage', 'fsaverage5', 'fsaverage6']), required=False)
@click.option('--fs-dir', default=None, required=False)
@click.option('--threshold', default=0., type=click.FLOAT, required=False)
def view_surf(file, hemi, space, fs_dir, threshold):
    """ Utility command to quickly view interactive surface in your browser. 
    
    file : str
        Path to numpy file with vertex data
    hemi : str
        Hemifield; either L or R
    space : str
        Space of vertices (fsaverage[,5,6])
    fs_dir : str
        Directory with space template (mutually exclusive with `space` param)
    threshold : float
        Minimum value to display
    """
    if fs_dir is not None:
        mesh = op.join(fs_dir, 'surf', f"{hemi.lower()}h.inflated")
        bg = op.join(fs_dir, 'surf', f"{hemi.lower()}h.sulc")
    else:
        hemi = 'left' if hemi == 'L' else 'right'
        fs = fetch_surf_fsaverage(mesh=space)
        mesh = fs[f"infl_{hemi}"]
        bg = fs[f"sulc_{hemi}"]
        
    dat = np.load(file)
    display = plotting.view_surf(
        surf_mesh=mesh,
        surf_map=dat,
        bg_map=bg,
        threshold=threshold
    )
    display.open_in_browser()


def get_run_data(ddict, run, func_type='preproc'):
    """ Get the data for a specific run. """
    t_idx = ddict['run_idx'] == run  # timepoint index
    func = ddict[f'{func_type}_func'][t_idx, :]
    conf = ddict['preproc_conf'].loc[t_idx, :].to_numpy()
    events = ddict['preproc_events'].query("run == (@run + 1)")

    # I think we need an explicit copy here (not sure)
    return func.copy(), conf.copy(), events.copy()
