import os
os.environ['TF_CPP_MIN_LOG_LEVEL']='2'
import os.path as osp
import random
import argparse
import numpy as np
from imageio import imread, imsave
import pickle

import dnnlib
import dnnlib.tflib as tflib

from util.utilgan import latent_anima, basename, img_read, img_list
try: # progress bar for notebooks 
    get_ipython().__class__.__name__
    from util.progress_bar import ProgressIPy as ProgressBar
except: # normal console
    from util.progress_bar import ProgressBar

desc = "Customized StyleGAN2 on Tensorflow"
parser = argparse.ArgumentParser(description=desc)
parser.add_argument('--out_dir', default='_out', help='output directory')
parser.add_argument('--model', default='models/ffhq-1024.pkl', help='path to pkl checkpoint file')
parser.add_argument('--size', default=None, help='output resolution, set in X-Y format')
parser.add_argument('--scale_type', default='pad', help="main types: pad, padside, symm, symmside")
parser.add_argument('--latmask', default=None, help='external mask file (or directory) for multi latent blending (overriding frame split method)')
parser.add_argument('--nXY', '-n', default='1-1', help='multi latent frame split count by X (width) and Y (height)')
parser.add_argument('--splitfine', type=float, default=0, help='multi latent frame split edge sharpness (0 = smooth, higher => finer)')
parser.add_argument('--trunc', type=float, default=0.8, help='truncation psi 0..1 (lower = stable, higher = various)')
parser.add_argument('--labels', default=None, help='labels/categories for conditioning')
parser.add_argument('--digress', type=float, default=0, help='distortion technique by Aydao (strength of the effect)') 
parser.add_argument('--save_lat', action='store_true', help='save latent vectors to file')
parser.add_argument('--verbose', '-v', action='store_true')
parser.add_argument('--ops', default='cuda', help='custom op implementation (cuda or ref)')
# animation
parser.add_argument('--frames', default='200-25', help='total frames to generate, length of interpolation step')
parser.add_argument("--cubic", action='store_true', help="use cubic splines for smoothing")
parser.add_argument("--gauss", action='store_true', help="use Gaussian smoothing")
a = parser.parse_args()

if a.size is not None: a.size = [int(s) for s in a.size.split('-')][::-1]
[a.frames, a.fstep] = [int(s) for s in a.frames.split('-')]

def main():
    os.makedirs(a.out_dir, exist_ok=True)
    seed = 696
    np.random.seed(seed=seed)

    # setup generator
    fmt = dict(func=tflib.convert_images_to_uint8, nchw_to_nhwc=True)
    Gs_kwargs = dnnlib.EasyDict()
    Gs_kwargs.func_name = 'training.stylegan2_multi.G_main'
    Gs_kwargs.verbose = a.verbose
    Gs_kwargs.size = a.size
    Gs_kwargs.scale_type = a.scale_type
    Gs_kwargs.impl = a.ops
    
    # mask/blend latents with external latmask or by splitting the frame
    if a.latmask is None:
        nHW = [int(s) for s in a.nXY.split('-')][::-1]
        assert len(nHW)==2, ' Wrong count nXY: %d (must be 2)' % len(nHW)
        n_mult = nHW[0] * nHW[1]
        if a.verbose is True and n_mult > 1: print(' Latent blending w/split frame %d x %d' % (nHW[1], nHW[0]))
        lmask = np.tile(np.asarray([[[[None]]]]), (1,n_mult,1,1))
        Gs_kwargs.countHW = nHW
        Gs_kwargs.splitfine = a.splitfine
    else:
        if a.verbose is True: print(' Latent blending with mask', a.latmask)
        n_mult = 2
        if osp.isfile(a.latmask): # single file
            lmask = np.asarray([[img_read(a.latmask)[:,:,0] / 255.]]) # [1,1,h,w]
        elif osp.isdir(a.latmask): # directory with frame sequence
            lmask = np.expand_dims(np.asarray([img_read(f)[:,:,0] / 255. for f in img_list(a.latmask)]), 1) # [n,1,h,w]
        else:
            print(' !! Blending mask not found:', a.latmask); exit(1)
        if a.verbose is True: print(' latmask shape', lmask.shape)
        lmask = np.concatenate((lmask, 1 - lmask), 1) # [n,2,h,w]
        Gs_kwargs.latmask_res = lmask.shape[2:]
    
    # load model with arguments
    sess = tflib.init_tf({'allow_soft_placement':True})
    pkl_name = osp.splitext(a.model)[0]
    with open(pkl_name + '.pkl', 'rb') as file:
        network = pickle.load(file, encoding='latin1')
    try: _, _, network = network
    except: pass
    for k in list(network.static_kwargs.keys()):
        Gs_kwargs[k] = network.static_kwargs[k]

    # reload custom network, if needed
    if '.pkl' in a.model.lower(): 
        print(' .. Gs from pkl ..', basename(a.model))
        Gs = network
    else: # reconstruct network
        print(' .. Gs custom ..', basename(a.model))
        # print(Gs_kwargs)

        Gs_kwargs['latent_size'] = 1024
        print(Gs_kwargs)

        Gs = tflib.Network('Gs', **Gs_kwargs)
        Gs.copy_vars_from(network)
    if a.verbose is True: print('kwargs:', ['%s: %s'%(kv[0],kv[1]) for kv in sorted(Gs.static_kwargs.items())])

    if a.verbose is True: print(' out shape', Gs.output_shape[1:])
    if a.size is None: a.size = Gs.output_shape[2:]

    if a.verbose is True: print(' making timeline..')
    lats = [] # list of [frm,1,512]
    for i in range(n_mult):
        lat_tmp = latent_anima((1, Gs.input_shape[1]), a.frames, a.fstep, cubic=a.cubic, gauss=a.gauss, verbose=False) # [frm,1,512]
        lats.append(lat_tmp) # list of [frm,1,512]
    latents = np.concatenate(lats, 1) # [frm,X,512]
    print(' latents', latents.shape)
    frame_count = latents.shape[0]
    
    # distort image by tweaking initial const layer
    if a.digress > 0:
        try: latent_size = Gs.static_kwargs['latent_size']
        except: latent_size = 512 # default latent size
        try: init_res = Gs.static_kwargs['init_res']
        except: init_res = (4,4) # default initial layer size 
        dconst = []
        for i in range(n_mult):
            dc_tmp = a.digress * latent_anima([1, latent_size, *init_res], a.frames, a.fstep, cubic=True, verbose=False)
            dconst.append(dc_tmp)
        dconst = np.concatenate(dconst, 1)
    else:
        dconst = np.zeros([frame_count, 1, 1, 1, 1])

    # labels / conditions
    try:
        label_size = Gs_kwargs.label_size
    except:
        label_size = 0
    if label_size > 0:
        labels = np.zeros((frame_count, n_mult, label_size)) # [frm,X,lbl]
        if a.labels is None:
            label_ids = []
            for i in range(n_mult):
                label_ids.append(random.randint(0, label_size-1))
        else:
            label_ids = [int(x) for x in a.labels.split('-')]
            label_ids = label_ids[:n_mult] # ensure we have enough labels
        for i, l in enumerate(label_ids):
            labels[:,i,l] = 1
    else:
        labels = [None]

    # generate images from latent timeline
    pbar = ProgressBar(frame_count)
    for i in range(frame_count):
    
        latent  = latents[i] # [X,512]
        label   = labels[i % len(labels)]
        latmask = lmask[i % len(lmask)] if lmask is not None else [None] # [X,h,w]
        dc      = dconst[i % len(dconst)] # [X,512,4,4]

        # generate multi-latent result
        if Gs.num_inputs == 2:
            output = Gs.run(latent, label, truncation_psi=a.trunc, randomize_noise=False, output_transform=fmt)
        else:
            output = Gs.run(latent, label, latmask, dc, truncation_psi=a.trunc, randomize_noise=False, output_transform=fmt)

        # save image
        ext = 'png' if output.shape[3]==4 else 'jpg'
        filename = osp.join(a.out_dir, "%06d.%s" % (i,ext))
        imsave(filename, output[0])
        pbar.upd()

    # convert latents to dlatents, save them
    if a.save_lat is True:
        latents = latents.squeeze(1) # [frm,512]
        dlatents = Gs.components.mapping.run(latents, label, dtype='float16') # [frm,18,512]
        filename = '{}-{}-{}.npy'.format(basename(a.model), a.size[1], a.size[0])
        filename = osp.join(osp.dirname(a.out_dir), filename)
        np.save(filename, dlatents)
        print('saved dlatents', dlatents.shape, 'to', filename)
        
if __name__ == '__main__':
    main()


    '''
    
    python3 src/_genSGAN2.py --model '/home/rednax/Desktop/music_vr/StyleGAN_training/stylegan2-ada/results/Dark_512_2/network-snapshot-000348' --out_dir '_out/Dark' --frames '50-10' --size 1024-512 --cubic
        
    python3 src/_genSGAN2.py --model '/home/rednax/Desktop/music_vr/StyleGAN_training/stylegan2-ada/results/Patterns_512/network-snapshot-000236' --out_dir '_out/Dark' --frames '200-24' --size 2048-1024 --cubic

    
    '''
