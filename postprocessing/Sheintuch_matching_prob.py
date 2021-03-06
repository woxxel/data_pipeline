import os, sys, cv2, math, time, warnings, logging
from tqdm import *
from itertools import chain
import numpy as np
from scipy.io import loadmat
import scipy as sp
from scipy.optimize import curve_fit, linear_sum_assignment
import matplotlib.pyplot as plt
from matplotlib import rc
from matplotlib import animation
from matplotlib import cm
import matplotlib.colors as mcolors

from mpl_toolkits.mplot3d import Axes3D


sys.path.append('/home/wollex/Data/Documents/Uni/2016-XXXX_PhD/Japan/Work/Programs/PC_analysis')
from utils import com, pathcat, calculate_img_correlation, get_shift_and_flow, fun_wrapper, pickleData, gmean

warnings.filterwarnings("ignore")


class Sheintuch_matching:

    def __init__(self,basePath,mouse,sessions,dataSet='redetect',SNR_thr=3.,r_thr=0.5,CNN_thr=0.6,s_corr_thr=0.3,d_thr=12,nbins=100,qtl=[0.05,0.95],use_kde=True,model='new'):

        self.para = {'pathMouse': pathcat([basePath,mouse]),
            'sessions':  list(chain.from_iterable(sessions)) if (type(sessions[0]) is range) else list(chain(sessions)),
            'fp_file':   'results_%s.mat'%dataSet,
            'nbins':     nbins,
            'd_thr':     d_thr,
            'qtl':       qtl,
            'dims':      (512,512),
            'pxtomu':    530.68/512,
            'SNR_thr':   SNR_thr,
            'r_thr':     r_thr,
            'CNN_thr':   CNN_thr,
            'session_correlation_thr':s_corr_thr,
            'use_kde':   use_kde,
            'model':     model         ## can be 'new', 'old', or 'both'
        }

        self.dataSet = dataSet
        self.nS = len(list(self.para['sessions']))

        self.data = {'nA':                np.zeros(self.nS,'int'),
                     'cm':                {},
                     'p_same':            {}}

        self.session_data = {'D_ROIs':[],
                             'fp_corr':[],
                             'fp_corr_max':[],
                             'nearest_neighbour':[],
                             'idx_eval':[]}

        self.model = {'counts':           np.zeros((self.para['nbins'],self.para['nbins'],3)),
            'counts_old':       np.zeros((self.para['nbins'],self.para['nbins'],3)),
            'counts_same':      np.zeros((self.para['nbins'],self.para['nbins'])),
            'counts_same_old':  np.zeros((self.para['nbins'],self.para['nbins'])),
            'fit_function':   {'distance':       {'NN':   [],
                                                'nNN':  [],
                                                'all':  []},
                             'fp_correlation': {'NN':   [],
                                                'nNN':  [],
                                                'all':  []}},
            'fit_parameter':  {'single':  {'distance':        {'NN':    [],
                                                             'nNN':   [],
                                                             'all':   []},
                                         'fp_correlation':  {'NN':    [],
                                                             'nNN':   [],
                                                             'all':   []}},
                             'joint': {}},
            'pdf':            {'single':  {'distance':[],
                                         'fp_correlation':[]},
                             'joint':   []},
            'p_same':         {'single':{},
                             'joint':[]},
            'kernel':         {'idxes':   {},
                             'kde':     {}}
        }

    def run_matching(self,p_thr=0.5):
        print('Now analyzing mouse %s'%self.para['pathMouse'])
        print('Building model for matching ...')
        self.run_analysis(save_results=True)
        print('Matching neurons ...')
        self.run_registration(save_results=True,p_thr=p_thr)
        print('Done!')


    def run_analysis(self,save_results=False,suffix=''):

        self.s_ref = 0
        self.progress = tqdm(enumerate(self.para['sessions']),total=self.nS)
        self.Cn_ref = None
        for (s,s0) in self.progress:
            # print(s,s0)
            self.A2 = self.load_footprints(s0,Cn_ref=self.Cn_ref)

            if not (self.A2 is None):
                if s>0:
                    c_max,_ = calculate_img_correlation(self.Cn_ref,self.Cn,plot_bool=False)
                    #_,_,_,c_max = get_shift_and_flow(self.A_ref,self.A2,self.para['dims'],projection=1,plot_bool=False)

                    self.progress.set_description('Aligning data from Session %d, c=%5.3g'%(s0,c_max))
                    if c_max < self.para['session_correlation_thr']:   ## don't include this session in matching
                        print('skipping session %s (correlation c=%5.3g too low)'%(s0,c_max))
                        continue
                    self.prepare_footprints()

                self.A_idx = np.squeeze(np.array((self.A2>0).sum(0)>50,'bool')) & self.session_data['idx_eval']
                self.data['nA'][s] = self.A2.shape[1]
                self.data['cm'][s] = com(self.A2,self.para['dims'][0],self.para['dims'][1]) * self.para['pxtomu']
                self.calculate_statistics(self.A2,self.A2,s,cm_ref=self.data['cm'][s],A_idx_ref=self.A_idx)
                self.A_idx = self.A_idx & self.session_data['idx_eval']

                self.progress.set_description('Calculate neuron positions for Session %d'%s0)
                self.data['cm'][s][~self.A_idx,:] = np.NaN    ## some empty footprints might be in here

                if self.para['use_kde']:
                    self.progress.set_description('Calculate kernel density for Session %d'%s0)
                    self.position_kde(self.A2,s,self.para['qtl'])         # build kernel

                self.update_joint_model(self.para['use_kde'],ds=0,s=s)
                if s>0:
                    self.progress.set_description('Calculate statistics for Session %d'%s0)
                    self.calculate_statistics(self.A_ref,self.A2,s)       # calculating distances and footprint correlations
                    self.progress.set_description('Update model with data from Session %d'%s0)
                    self.update_joint_model(self.para['use_kde'])

                self.A_ref = self.A2.copy()
                self.A_idx_ref = self.A_idx.copy()
                self.s_ref = s
                self.Cn_ref = self.Cn.copy()

        self.fit_model()

        if save_results:
            self.save_model(suffix=suffix)


    def run_registration(self, p_thr=0.5, plot_results=False, save_results=False, save_suffix='',model='new'):

        self.para['model'] = model

        d_arr = np.linspace(0,self.para['d_thr'],self.para['nbins']+1)[:-1]
        d_arr += np.diff(d_arr)[0]/2
        fp_arr = np.linspace(0,1,self.para['nbins']+1)[:-1]
        fp_arr += np.diff(fp_arr)[0]/2

        self.f_same = sp.interpolate.RectBivariateSpline(d_arr,fp_arr,self.model['p_same']['joint'])

        self.progress = tqdm(zip(self.para['sessions'][1:],range(1,self.nS)),total=self.nS,leave=True)
        self.A0 = self.load_footprints(self.para['sessions'][0])
        # print(self.A0.shape)
        # print(self.data[''])
        self.data['cm'][0] = com(self.A0,self.para['dims'][0],self.para['dims'][1]) * self.para['pxtomu']

        self.A_idx = np.squeeze(np.array((self.A0>0).sum(0)>50,'bool')) & self.session_data['idx_eval']
        self.data['nA'][0] = self.A0.shape[1]
        self.calculate_statistics(self.A0,self.A0,0,cm_ref=self.data['cm'][0],A_idx_ref=self.A_idx)
        self.A_idx = self.A_idx & self.session_data['idx_eval']
        self.data['nA'][0] = self.A_idx.sum()

        self.A_ref = self.A0[:,self.A_idx]
        A2_ref = self.A_ref.copy()
        self.s_ref = 0
        self.Cn_ref = self.Cn.copy()

        self.assignments = np.zeros((self.data['nA'][0],self.nS))*np.NaN
        self.assignments[:self.data['nA'][0],0] = np.where(self.A_idx)[0]
        self.p_matched = np.zeros((self.data['nA'][0],self.nS))
        self.p_matched[:,0] = np.NaN

        for (s0,s) in self.progress:

            self.nA_ref = self.A_ref.shape[1]
            self.cm_ref = com(self.A_ref,self.para['dims'][0],self.para['dims'][1]) * self.para['pxtomu']

            self.A2 = self.load_footprints(s0,self.Cn_ref)
            if not (self.A2 is None):
                self.A_idx_ref = np.squeeze(np.array((self.A_ref>0).sum(0)>50,'bool')) & np.ones(self.nA_ref,'bool')

                #tqdm.write('Session %d data contains %d neurons'%(s0))
                #_,_,_,c_max = get_shift_and_flow(A2_ref,self.A2,self.para['dims'],projection=1,plot_bool=False)
                c_max,_ = calculate_img_correlation(self.Cn_ref,self.Cn,plot_bool=False)

                self.progress.set_description('A union size: %d, Aligning data from Session %d, c=%5.3g'%(self.nA_ref,s0,c_max))

                if c_max < self.para['session_correlation_thr']:   ## don't include this session in matching
                    print('Correlation c=%5.3g too low, skip session %d'%(c_max,s0))
                    continue

                self.prepare_footprints(A_ref=self.A0)
                self.data['nA'][s] = self.A2.shape[1]
                self.data['cm'][s] = com(self.A2,self.para['dims'][0],self.para['dims'][1]) * self.para['pxtomu']
                self.A_idx = np.squeeze(np.array((self.A2>0).sum(0)>50,'bool')) & self.session_data['idx_eval']
                self.calculate_statistics(self.A2,self.A2,s,cm_ref=self.data['cm'][s],A_idx_ref=self.A_idx)
                self.A_idx = self.A_idx & self.session_data['idx_eval']

                self.data['cm'][s][~self.A_idx,:] = np.NaN    ## some empty footprints might be in here

                self.progress.set_description('A union size: %d, Calculate statistics for Session %d'%(self.nA_ref,s0))
                self.calculate_statistics(self.A_ref,self.A2,s,cm_ref=self.cm_ref)

                self.progress.set_description('A union size: %d, Obtaining matching probability for Session %d'%(self.nA_ref,s0))
                self.data['p_same'][s] = self.calculate_p()

                #run hungarian matching with these (1-p_same) as score
                self.progress.set_description('A union size: %d, Perform Hungarian matching on Session %d'%(self.nA_ref,s0))
                matches, p_matched = self.find_matches(s, p_thr=p_thr, plot_results=plot_results)

                idx_TP = np.where(p_matched > p_thr)[0] ## thresholding results
                if len(idx_TP) > 0:
                    matched_ref = matches[0][idx_TP]    # ground truth
                    matched2 = matches[1][idx_TP]   # algorithm - comp
                    non_matched_ref = np.setdiff1d(list(range(self.nA_ref)), matches[0][idx_TP])
                    non_matched2 = np.setdiff1d(list(np.where(self.A_idx)[0]), matches[1][idx_TP])  #range(self.data['nA'][s])
                    non_matched2 = non_matched2[self.A_idx[non_matched2]]
                    TP = np.sum(p_matched > p_thr).astype('float32')

                self.A_ref = self.A_ref.tolil()
                #self.A_ref[:,mat_un] = w*self.A_ref[:,matched_ref] + (1-w)*self.A2[:,matched2]
                self.A_ref[:,matched_ref] = self.A_ref[:,matched_ref].multiply(1-p_matched[idx_TP]/2) + self.A2[:,matched2].multiply(p_matched[idx_TP]/2)
                self.A_ref = sp.sparse.hstack([self.A_ref, self.A2[:,non_matched2]]).asformat('csc')

                N_add = len(non_matched2)

                self.assignments[matched_ref,s] = matched2
                match_add = np.zeros((N_add,self.nS))*np.NaN
                match_add[:,s] = non_matched2
                self.assignments = np.vstack([self.assignments,match_add])

                self.p_matched[matched_ref,s] = p_matched[idx_TP]
                p_same_add = np.zeros((N_add,self.nS))*np.NaN
                self.p_matched = np.vstack([self.p_matched,p_same_add])

                A2_ref = self.A2.copy()
                self.s_ref = s
                self.Cn_ref = self.Cn.copy()

        if save_results:
            self.save_registration(suffix=save_suffix)



    def calculate_p(self):

        p_same = np.zeros(self.session_data['D_ROIs'].shape)

        close_neighbours = self.session_data['D_ROIs'] < 8#self.para['d_thr']
        if self.para['model'] == 'new':
            p_same[close_neighbours] = self.f_same.ev(self.session_data['D_ROIs'][close_neighbours],self.session_data['fp_corr_max'][close_neighbours])
        else:
            p_same[close_neighbours] = self.f_same.ev(self.session_data['D_ROIs'][close_neighbours],self.session_data['fp_corr'][close_neighbours])

        p_same[~self.A_idx_ref,:] = 0
        p_same[:,~self.A_idx] = 0

        #p_same[np.where((self.A_ref>0).sum(0)<50)[1],:] = 0
        p_same[p_same<0] = 0
        p_same[p_same>1] = 1


        #d_arr = np.linspace(0,self.para['d_thr'],self.para['nbins']+1)[:-1]
        #fp_arr = np.linspace(0,1,self.para['nbins']+1)[:-1]

        #d_w = np.diff(d_arr)[0]
        #fp_w = np.diff(fp_arr)[0]

        #D_idx = (self.session_data['D_ROIs'][close_neighbours] / d_w).astype('int')
        #if self.para['model'] == 'new':
          #c_idx = (self.session_data['fp_corr_max'][close_neighbours] / fp_w).astype('int')
          #idx_mask = np.isnan(self.session_data['fp_corr_max'][close_neighbours])
        #else:
          #c_idx = (self.session_data['fp_corr'][close_neighbours] / fp_w).astype('int')
          #idx_mask = np.isnan(self.session_data['fp_corr'][close_neighbours])

        #c_idx[idx_mask] = 0


        #p_same[close_neighbours] = self.model['p_same']['joint'][D_idx,c_idx]
        #if self.para['model'] == 'new':
          #p_same[np.isnan(self.session_data['fp_corr_max'])] = 0
        #if self.para['model'] == 'old':
          #p_same[np.isnan(self.session_data['fp_corr'])] = 0

        return sp.sparse.csc_matrix(p_same)


    def find_matches(self, s, p_thr=0.5,plot_results=False):

        matches = linear_sum_assignment(1 - self.data['p_same'][s].toarray())
        p_matched = self.data['p_same'][s].toarray()[matches]


        #print(performance)
        #print('are all empty neurons removed?')
        #print([((self.A_ref!=0).sum(0)<50).sum(),((self.A2!=0).sum(0)<50).sum()])

        if plot_results:
            idx_TP = np.where(np.array(p_matched) > p_thr)[0] ## thresholding results
            if len(idx_TP) > 0:
                matched_ROIs1 = matches[0][idx_TP]    # ground truth
                matched_ROIs2 = matches[1][idx_TP]   # algorithm - comp
                non_matched1 = np.setdiff1d(list(range(self.nA_ref)), matches[0][idx_TP])
                non_matched2 = np.setdiff1d(list(range(self.data['nA'][s])), matches[1][idx_TP])
                TP = np.sum(np.array(p_matched) > p_thr).astype('float32')
            else:
                TP = 0.
                plot_results = False
                matched_ROIs1 = []
                matched_ROIs2 = []
                non_matched1 = list(range(self.nA_ref))
                non_matched2 = list(range(self.data['nA'][s]))

                FN = self.nA_ref - TP
                FP = self.data['nA'][s] - TP
                TN = 0

                performance = dict()
                performance['recall'] = TP / (TP + FN)
                performance['precision'] = TP / (TP + FP)
                performance['accuracy'] = (TP + TN) / (TP + FP + FN + TN)
                performance['f1_score'] = 2 * TP / (2 * TP + FP + FN)

            print('plotting...')
            t_start = time.time()
            cmap = 'viridis'

            Cn = self.A_ref.sum(1).reshape(512,512)

            #        try : #Plotting function
            level = 0.1
            plt.figure(figsize=(15,12))
            plt.rcParams['pdf.fonttype'] = 42
            font = {'family': 'Myriad Pro',
                  'weight': 'regular',
                  'size': 10}
            plt.rc('font', **font)
            lp, hp = np.nanpercentile(Cn, [5, 95])

            ax_matches = plt.subplot(121)
            ax_nonmatches = plt.subplot(122)

            ax_matches.imshow(Cn, vmin=lp, vmax=hp, cmap=cmap)
            ax_nonmatches.imshow(Cn, vmin=lp, vmax=hp, cmap=cmap)

            A = np.reshape(self.A_ref.astype('float32').toarray(), self.para['dims'] + (-1,), order='F').transpose(2, 0, 1)
            [ax_matches.contour(a, levels=[level], colors='w', linewidths=1) for a in A[matched_ROIs1,...]]
            [ax_nonmatches.contour(a, levels=[level], colors='w', linewidths=1) for a in A[non_matched1,...]]

            print('first half done - %5.3f'%(t_end-t_start))
            A = None
            A = np.reshape(self.A2.astype('float32').toarray(), self.para['dims'] + (-1,), order='F').transpose(2, 0, 1)

            [ax_matches.contour(a, levels=[level], colors='r', linewidths=1) for a in A[matched_ROIs2,...]]
            [ax_nonmatches.contour(a, levels=[level], colors='r', linewidths=1) for a in A[non_matched2,...]]
            A = None

            plt.draw()
            t_end = time.time()
            print('done. time taken: %5.3f'%(t_end-t_start))
            plt.show(block=False)
           #except Exception as e:
    #            logging.warning("not able to plot precision recall usually because we are on travis")
    #            logging.warning(e)

        return matches, p_matched


    def load_footprints(self,s,Cn_ref=None):

        pathData = pathcat([self.para['pathMouse'],'Session%02d'%s,self.para['fp_file']])
        # print(pathData)
        # pathBG = pathcat([self.para['pathMouse'],'Session%02d/results_OnACID.mat'])
        if os.path.exists(pathData):
            if self.dataSet == 'redetect':
                ld = loadmat(pathData,variable_names=['A','C','idx_evaluate','SNR','r_values','CNN'],squeeze_me=True)
                self.Cn = np.array(ld['A'].sum(1).reshape(512,512))
                self.session_data['SNR'] = ld['SNR']
                self.session_data['C'] = ld['C']
                self.session_data['idx_eval'] = (ld['SNR']>self.para['SNR_thr']) & (ld['r_values']>self.para['r_thr']) & (ld['CNN']>self.para['CNN_thr'])
            else:
                ld = loadmat(pathData,variable_names=['A'],squeeze_me=True)
                self.Cn = np.array(ld['A'].sum(1).reshape(512,512))
                # self.session_data['C'] = ld['C']
                self.session_data['idx_eval'] = np.ones(ld['A'].shape[1],'bool')
            A = ld['A']
            if not (Cn_ref is None):
                c_max,_ = calculate_img_correlation(Cn_ref,self.Cn,plot_bool=False)
                c_max_T,_ = calculate_img_correlation(Cn_ref,self.Cn.T,plot_bool=False)
                if (c_max_T > c_max) & (c_max_T > self.para['session_correlation_thr']):
                    print('Transposed image in Session %d'%s)
                    A = sp.sparse.hstack([img.reshape(self.para['dims']).transpose().reshape(-1,1) for img in A.transpose()])
                    self.Cn = np.array(A.sum(1).reshape(512,512))
            return A
        else:
            return None

    def prepare_footprints(self,A_ref=None,align_flag=True,use_opt_flow=True,max_thr=0.001):

        if A_ref is None:
            A_ref = self.A_ref

        if 'csc_matrix' not in str(type(A_ref)):
            A_ref = sp.sparse.csc_matrix(A_ref)
        if 'csc_matrix' not in str(type(self.A2)):
            self.A2 = sp.sparse.csc_matrix(self.A2)

        if align_flag:  # first align ROIs from session 2 to the template from session 1
            t_start = time.time()

            (x_shift,y_shift),flow,(x_grid,y_grid),_ = get_shift_and_flow(A_ref,self.A2,self.para['dims'],projection=1,plot_bool=False)

            if use_opt_flow:    ## for each pixel, find according position in other map
                x_remap = (x_grid - x_shift + flow[:,:,0])
                y_remap = (y_grid - y_shift + flow[:,:,1])
            else:
                x_remap = (x_grid - x_shift)
                y_remap = (y_grid - y_shift)

            self.A2 = sp.sparse.hstack([sp.sparse.csc_matrix(cv2.remap(img.reshape(self.para['dims']), x_remap,y_remap, cv2.INTER_CUBIC).reshape(-1,1)) for img in self.A2.toarray().T])

        self.A_ref = sp.sparse.vstack([a.multiply(a>max_thr*a.max())/a.max() if (a>0).sum()>50 else sp.sparse.csr_matrix(a.shape) for a in self.A_ref.T]).T
        self.A2 = sp.sparse.vstack([a.multiply(a>max_thr*a.max())/a.max() if (a>0).sum()>50 else sp.sparse.csr_matrix(a.shape) for a in self.A2.T]).T


  def calculate_statistics(self,A1,A2,s,cm_ref=None,A_idx_ref=None,binary='half'):

    if cm_ref is None:
        cm_ref = self.data['cm'][self.s_ref]

    if A_idx_ref is None:
        A_idx_ref = self.A_idx_ref
    nA_ref = cm_ref.shape[0]

    sameA = True if np.all(A1.sum(1)==A2.sum(1)) else False

    self.session_data['D_ROIs'] = sp.spatial.distance.cdist(cm_ref,self.data['cm'][s])
    self.session_data['fp_corr'] = np.zeros((nA_ref,self.data['nA'][s]))*np.NaN
    self.session_data['fp_corr_max'] = np.zeros((nA_ref,self.data['nA'][s]))*np.NaN

    self.session_data['nearest_neighbour'] = np.zeros((nA_ref,self.data['nA'][s]),'bool')

    idx_good = np.where(~np.isnan(cm_ref[:,0]))[0]
    idx_NN = np.nanargmin(self.session_data['D_ROIs'][idx_good,:],axis=1)
    self.session_data['nearest_neighbour'][idx_good,idx_NN] = True
    c_rm = 0
    for i in tqdm(range(nA_ref),desc='calculating footprint correlation of %d neurons'%nA_ref,leave=False):
        if A_idx_ref[i]:#A1[:,i].sum()>0:
            for j in np.where(self.session_data['D_ROIs'][i,:]<self.para['d_thr'])[0]:#
                if self.A_idx[j]:
                # try:
                    if (self.para['model']=='old') | (self.para['model']=='both'):
                        self.session_data['fp_corr'][i,j], shift = calculate_img_correlation(A1[:,i],A2[:,j],shift=False)

                    if (self.para['model']=='new') | (self.para['model']=='both'):
                        self.session_data['fp_corr_max'][i,j], shift = calculate_img_correlation(A1[:,i],A2[:,j],crop=True,shift=True,binary=binary)

                        if ('SNR' in self.session_data.keys()) & sameA & (i!=j) & (self.session_data['fp_corr_max'][i,j] > 0.2):
                            C_corr = np.corrcoef(self.session_data['C'][i,:],self.session_data['C'][j,:])[0,1]
                            if C_corr > 0.5:
                                idx_remove = j if self.session_data['SNR'][i]>self.session_data['SNR'][j] else i
                                self.session_data['idx_eval'][idx_remove] = False

                                # if self.session_data['SNR'][i]>self.session_data['SNR'][j]:
                                #     self.session_data['idx_eval'][j] = False
                                #     # print('removing neuron %d from data (Acorr: %.3f, Ccorr: %.3f; SNR: %.2f vs %.2f)'%(i,self.session_data['fp_corr_max'][i,j],C_corr,self.session_data['SNR'][i],self.session_data['SNR'][j]))
                                # else:
                                #     self.session_data['idx_eval'][i] = False
                                #     # print('removing neuron %d from data (Acorr: %.3f, Ccorr: %.3f; SNR: %.2f vs %.2f)'%(j,self.session_data['fp_corr_max'][i,j],C_corr,self.session_data['SNR'][i],self.session_data['SNR'][j]))
                                c_rm += 1
                                self.session_data['fp_corr_max'][i,j] = np.NaN

            # except:
              # raise Exception('correlation calculation failed for neurons [%d,%d]'%(i,j))
    #self.session_data['fp_corr'].tocsc()
    #self.session_data['fp_corr_max'].tocsc()
    # print('%d neurons removed'%c_rm)

  def update_joint_model(self,use_kde,ds=1,s=None):

    distance_arr = np.linspace(0,self.para['d_thr'],self.para['nbins']+1)
    fpcorr_arr = np.linspace(0,1,self.para['nbins']+1)
    if use_kde:
      if ds>0:
        idxes = self.model['kernel']['idxes'][self.s_ref]
      else:
        idxes = self.model['kernel']['idxes'][s]
    else:
      idxes = np.ones(self.session_data['D_ROIs'].shape[0],'bool')

    ROI_close = self.session_data['D_ROIs'][idxes,:] < self.para['d_thr']

    NN_idx = self.session_data['nearest_neighbour'][idxes,:][ROI_close]
    D_ROIs = self.session_data['D_ROIs'][idxes,:][ROI_close]
    fp_corr = self.session_data['fp_corr'][idxes,:][ROI_close]
    fp_corr_max = self.session_data['fp_corr_max'][idxes,:][ROI_close]

    for i in tqdm(range(self.para['nbins']),desc='updating joint model',leave=False):
      idx_dist = (D_ROIs >= distance_arr[i]) & (D_ROIs < distance_arr[i+1])

      for j in range(self.para['nbins']):
        if (self.para['model']=='old') | (self.para['model']=='both'):
          idx_fp = (fp_corr > fpcorr_arr[j]) & (fpcorr_arr[j+1] > fp_corr)
          idx_vals = idx_dist & idx_fp
          if ds>0:
            self.model['counts_old'][i,j,0] += np.count_nonzero(idx_vals)
            self.model['counts_old'][i,j,1] += np.count_nonzero(idx_vals & NN_idx)
            self.model['counts_old'][i,j,2] += np.count_nonzero(idx_vals & ~NN_idx)
          else:
            self.model['counts_same_old'][i,j] += np.count_nonzero(idx_vals & ~NN_idx)

        if (self.para['model']=='new') | (self.para['model']=='both'):
          idx_fp = (fp_corr_max > fpcorr_arr[j]) & (fpcorr_arr[j+1] > fp_corr_max)
          idx_vals = idx_dist & idx_fp
          if ds>0:
            self.model['counts'][i,j,0] += np.count_nonzero(idx_vals)
            self.model['counts'][i,j,1] += np.count_nonzero(idx_vals & NN_idx)
            self.model['counts'][i,j,2] += np.count_nonzero(idx_vals & ~NN_idx)
          else:
            self.model['counts_same'][i,j] += np.count_nonzero(idx_vals & ~NN_idx)

  def position_kde(self,A,s,qtl=[0.05,0.95],plot_bool=False):

    #print('calculating kernel density estimates for session %d'%s)
    x_grid, y_grid = np.meshgrid(np.linspace(0,self.para['dims'][0]*self.para['pxtomu'],self.para['dims'][0]), np.linspace(0,self.para['dims'][1]*self.para['pxtomu'],self.para['dims'][1]))
    positions = np.vstack([x_grid.ravel(), y_grid.ravel()])
    kde = sp.stats.gaussian_kde(self.data['cm'][s][self.A_idx,:].T)
    self.model['kernel']['kde'][s] = np.reshape(kde(positions),x_grid.shape)

    cm_px = (self.data['cm'][s][self.A_idx,:]/self.para['pxtomu']).astype('int')
    kde_at_cm = np.zeros(self.data['nA'][s])*np.NaN
    kde_at_cm[self.A_idx] = self.model['kernel']['kde'][s][cm_px[:,1],cm_px[:,0]]
    self.model['kernel']['idxes'][s] = (kde_at_cm > np.quantile(self.model['kernel']['kde'][s],qtl[0])) & (kde_at_cm < np.quantile(self.model['kernel']['kde'][s],qtl[1]))

    if plot_bool:
      plt.figure()
      h_kde = plt.imshow(self.model['kernel']['kde'][s],cmap=plt.cm.gist_earth_r,origin='lower',extent=[0,self.para['dims'][0]*self.para['pxtomu'],0,self.para['dims'][1]*self.para['pxtomu']])
      #if s>0:
        #col = self.session_data['D_ROIs'].min(1)
      #else:
        #col = 'w'
      plt.scatter(self.data['cm'][s][:,0],self.data['cm'][s][:,1],c='w',s=5+10*self.model['kernel']['idxes'][s],clim=[0,10],cmap='YlOrRd')
      plt.xlim([0,self.para['dims'][0]*self.para['pxtomu']])
      plt.ylim([0,self.para['dims'][1]*self.para['pxtomu']])

      cm_px = (self.data['cm'][s]/self.para['pxtomu']).astype('int')
      kde_at_cm = self.model['kernel']['kde'][s][cm_px[:,1],cm_px[:,0]]
      plt.colorbar(h_kde)
      plt.show(block=False)


  def fit_model(self,count_thr=0,model='new'):

    self.para['model'] = model
    if (not (self.para['model'] == 'old')) & (not (self.para['model']=='new')):
      raise Exception('Please specify model to be either "new" or "old"')

    key_counts = 'counts' if self.para['model']=='new' else 'counts_old'

    nbins = self.para['nbins']

    self.set_functions()

    d_arr = np.linspace(0,self.para['d_thr'],nbins+1)[1:]
    d_w = np.diff(d_arr)[0]
    fp_arr = np.linspace(0,1,nbins+1)[:-1]
    fp_w = np.diff(fp_arr)[0]

    bounds_p = np.array([(0,1)]).T
    bounds_d_NN = np.array([(0,np.inf),(-np.inf,np.inf)]).T
    bounds_d_nNN = np.array([(0,np.inf),(0,np.inf),(0,self.para['d_thr']/2)]).T
    bounds_d = np.hstack([bounds_p,bounds_d_NN,bounds_d_nNN])

    bounds_corr_NN = np.array([(0,np.inf),(-np.inf,np.inf)]).T
    if self.para['model']=='old':
      bounds_corr_nNN = np.array([(-np.inf,np.inf),(-np.inf,np.inf)]).T
    else:
      bounds_corr_nNN = np.array([(0,np.inf),(-np.inf,np.inf)]).T
    bounds_corr = np.hstack([bounds_p,bounds_corr_NN,bounds_corr_nNN])

    ## build single models
    ### distance
    distance_NN_dat = self.model[key_counts][...,1].sum(1)/self.model[key_counts][...,1].sum()/d_w
    distance_nNN_dat = self.model[key_counts][...,2].sum(1)/self.model[key_counts][...,2].sum()/d_w
    distance_joint_dat = self.model[key_counts][...,0].sum(1)/self.model[key_counts][...,0].sum()/d_w
    self.model['fit_parameter']['single']['distance']['NN'] = curve_fit(self.model['fit_function']['distance']['NN'],d_arr,distance_NN_dat,bounds=bounds_d_NN)[0]
    self.model['fit_parameter']['single']['distance']['nNN'] = curve_fit(self.model['fit_function']['distance']['nNN'],d_arr,distance_nNN_dat,bounds=bounds_d_nNN)[0]
    p0 = (self.model[key_counts][...,1].sum()/self.model[key_counts][...,0].sum(),)+tuple(self.model['fit_parameter']['single']['distance']['NN'])+tuple(self.model['fit_parameter']['single']['distance']['nNN'])
    self.model['fit_parameter']['single']['distance']['all'] = curve_fit(self.model['fit_function']['distance']['all'],d_arr,distance_joint_dat,bounds=bounds_d,p0=p0)[0]


    ### to fp-correlation: NN - reverse lognormal, nNN - reverse lognormal
    fp_correlation_NN_dat = self.model[key_counts][...,1].sum(0)/self.model[key_counts][...,1].sum()/fp_w
    fp_correlation_nNN_dat = self.model[key_counts][...,2].sum(0)/self.model[key_counts][...,2].sum()/fp_w
    fp_correlation_joint_dat = self.model[key_counts][...,0].sum(0)/self.model[key_counts][...,0].sum()/fp_w
    self.model['fit_parameter']['single']['fp_correlation']['NN'] = curve_fit(self.model['fit_function']['fp_correlation']['NN'],fp_arr,fp_correlation_NN_dat,bounds=bounds_corr_NN)[0]
    self.model['fit_parameter']['single']['fp_correlation']['nNN'] = curve_fit(self.model['fit_function']['fp_correlation']['nNN'],fp_arr,fp_correlation_nNN_dat,bounds=bounds_corr_nNN)[0]
    p0 = (self.model[key_counts][...,1].sum()/self.model[key_counts][...,0].sum(),)+tuple(self.model['fit_parameter']['single']['fp_correlation']['NN'])+tuple(self.model['fit_parameter']['single']['fp_correlation']['nNN'])

    self.model['fit_parameter']['single']['fp_correlation']['all'] = curve_fit(self.model['fit_function']['fp_correlation']['all'],fp_arr,fp_correlation_joint_dat,bounds=bounds_corr,p0=p0)[0]

    d_NN = fun_wrapper(self.model['fit_function']['distance']['NN'],d_arr,self.model['fit_parameter']['single']['distance']['all'][1:3])*self.model['fit_parameter']['single']['distance']['all'][0]
    d_total = fun_wrapper(self.model['fit_function']['distance']['all'],d_arr,self.model['fit_parameter']['single']['distance']['all'])
    self.model['p_same']['single']['distance'] = d_NN/d_total

    corr_NN = fun_wrapper(self.model['fit_function']['fp_correlation']['NN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['all'][1:3])*self.model['fit_parameter']['single']['fp_correlation']['all'][0]
    corr_total = fun_wrapper(self.model['fit_function']['fp_correlation']['all'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['all'])
    self.model['p_same']['single']['fp_correlation'] = corr_NN/corr_total

    ## build joint model
    # preallocate
    self.model['fit_parameter']['joint'] = {
      'distance':{'NN':np.zeros((self.para['nbins'],len(self.model['fit_parameter']['single']['distance']['NN'])))*np.NaN,
                  'nNN':np.zeros((self.para['nbins'],2))*np.NaN},
                  #'all':np.zeros((self.para['nbins'],len(self.model['fit_parameter']['single']['distance']['all'])))*np.NaN},
      'fp_correlation':{'NN':np.zeros((self.para['nbins'],len(self.model['fit_parameter']['single']['fp_correlation']['NN'])))*np.NaN,
                        'nNN':np.zeros((self.para['nbins'],2))*np.NaN}
                        #'all':np.zeros((self.para['nbins'],len(self.model['fit_parameter']['single']['fp_correlation']['all'])))*np.NaN}
      }

    joint_hist_norm_dist = self.model[key_counts]/self.model[key_counts].sum(0)
    joint_hist_norm_dist[np.isnan(joint_hist_norm_dist)] = 0
    joint_hist_norm_corr = self.model[key_counts]/self.model[key_counts].sum(1)[:,np.newaxis,:]
    joint_hist_norm_corr[np.isnan(joint_hist_norm_corr)] = 0

    bounds_d_nNN = np.array([(0,np.inf),(-np.inf,np.inf)]).T
    bounds_corr_nNN = np.array([(0,np.inf),(-np.inf,np.inf)]).T
    counts_thr = 100
    for i in tqdm(range(nbins)):
      ### to distance distribution: NN - lognormal, nNN - large lognormal?!
      if self.model[key_counts][:,i,1].sum() > counts_thr:
        dat = joint_hist_norm_dist[:,i,1]/joint_hist_norm_dist[:,i,1].sum()*nbins/self.para['d_thr']
        self.model['fit_parameter']['joint']['distance']['NN'][i,:] = curve_fit(self.model['fit_function']['distance']['NN'],d_arr,dat,bounds=bounds_d_NN)[0]
      #else:
        #self.model['fit_parameter']['joint']['distance']['NN'][i,:] = 0
      if self.model[key_counts][:,i,2].sum() > counts_thr:
        dat = joint_hist_norm_dist[:,i,2]/joint_hist_norm_dist[:,i,2].sum()*nbins/self.para['d_thr']
        self.model['fit_parameter']['joint']['distance']['nNN'][i,:] = curve_fit(self.model['fit_function']['distance']['nNN_joint'],d_arr,dat,bounds=bounds_d_nNN)[0]

      ### to fp-correlation: NN - reverse lognormal, nNN - reverse lognormal
      if self.model[key_counts][i,:,1].sum() > counts_thr:
        dat = joint_hist_norm_corr[i,:,1]/joint_hist_norm_corr[i,:,1].sum()*nbins
        self.model['fit_parameter']['joint']['fp_correlation']['NN'][i,:] = curve_fit(self.model['fit_function']['fp_correlation']['NN'],fp_arr,dat,bounds=bounds_corr_NN)[0]
      #else:
        #self.model['fit_parameter']['joint']['fp_correlation']['NN'][i,:] = 0

      if self.model[key_counts][i,:,2].sum() > counts_thr:
        dat = joint_hist_norm_corr[i,:,2]/joint_hist_norm_corr[i,:,2].sum()*nbins
        self.model['fit_parameter']['joint']['fp_correlation']['nNN'][i,:] = curve_fit(self.model['fit_function']['fp_correlation']['nNN_joint'],fp_arr,dat,bounds=bounds_corr_nNN)[0]
      #else:
        #self.model['fit_parameter']['joint']['fp_correlation']['nNN'][i,:] = 0


    arrays = {'distance':         d_arr,
              'fp_correlation':   fp_arr}

    ## smooth parameter functions
    for key in ['distance','fp_correlation']:
      for pop in ['NN','nNN']:#,'all']
        #for ax in range(self.model['fit_parameter']['joint'][key][pop].shape(1)):
        self.model['fit_parameter']['joint'][key][pop] = sp.ndimage.median_filter(self.model['fit_parameter']['joint'][key][pop],[5,1])
        self.model['fit_parameter']['joint'][key][pop] = sp.ndimage.gaussian_filter(self.model['fit_parameter']['joint'][key][pop],[1,0])

        for ax in range(self.model['fit_parameter']['joint'][key][pop].shape[1]):
          ## find first/last index, at which parameter has a non-nan value
          nan_idx = np.isnan(self.model['fit_parameter']['joint'][key][pop][:,ax])
          if nan_idx[0]:  ## interpolate beginning
            idx = np.where(~nan_idx)[0][:20]
            y_arr = self.model['fit_parameter']['joint'][key][pop][idx,ax]
            f_interp = np.polyfit(arrays[key][idx],y_arr,1)
            fit_fun = np.poly1d(f_interp)

            self.model['fit_parameter']['joint'][key][pop][:idx[0],ax] = fit_fun(arrays[key][:idx[0]])
          if nan_idx[-1]:  ## interpolate beginning
            idx = np.where(~nan_idx)[0][-20:]
            y_arr = self.model['fit_parameter']['joint'][key][pop][idx,ax]
            f_interp = np.polyfit(arrays[key][idx],y_arr,1)
            fit_fun = np.poly1d(f_interp)

            self.model['fit_parameter']['joint'][key][pop][idx[-1]+1:,ax] = fit_fun(arrays[key][idx[-1]+1:])


    #np.nanmean(self.model['fit_parameter']['joint']['fp_correlation']['nNN'])
    #idxes = np.any(np.isnan(self.model['fit_parameter']['joint']['fp_correlation']['nNN']),1)
    #self.model['fit_parameter']['joint']['fp_correlation']['nNN'][idxes,:] = self.model['fit_parameter']['single']['fp_correlation']['nNN']
    #idxes = np.any(np.isnan(self.model['fit_parameter']['joint']['fp_correlation']['NN']),1)
    #self.model['fit_parameter']['joint']['fp_correlation']['NN'][idxes,:] = self.model['fit_parameter']['single']['fp_correlation']['nNN']

    ## define probability density functions
    self.model['pdf']['joint'] = np.zeros((2,nbins,nbins))
    for n in range(nbins):
      if not np.any(np.isnan(self.model['fit_parameter']['joint']['fp_correlation']['NN'][n,:])):
        f_NN = fun_wrapper(self.model['fit_function']['fp_correlation']['NN'],fp_arr,self.model['fit_parameter']['joint']['fp_correlation']['NN'][n,:])
        self.model['pdf']['joint'][0,n,:] = f_NN*self.model['p_same']['single']['distance'][n]
    for n in range(nbins):
      if not np.any(np.isnan(self.model['fit_parameter']['joint']['fp_correlation']['nNN'][n,:])):
        f_nNN = fun_wrapper(self.model['fit_function']['fp_correlation']['nNN_joint'],fp_arr,self.model['fit_parameter']['joint']['fp_correlation']['nNN'][n,:])
        self.model['pdf']['joint'][1,n,:] = f_nNN*(1-self.model['p_same']['single']['distance'][n])

    #self.model['pdf']['joint'] = np.zeros((2,nbins,nbins))
    #for n in range(nbins):
      #if not np.any(np.isnan(self.model['fit_parameter']['joint']['distance']['NN'][n,:])):
        #f_NN = fun_wrapper(self.model['fit_function']['distance']['NN'],d_arr,self.model['fit_parameter']['joint']['distance']['NN'][n,:])
        #self.model['pdf']['joint'][0,:,n] = f_NN*self.model['p_same']['single']['fp_correlation'][n]
    #for n in range(nbins):
      #if not np.any(np.isnan(self.model['fit_parameter']['joint']['distance']['nNN'][n,:])):
        #f_nNN = fun_wrapper(self.model['fit_function']['distance']['nNN_joint'],d_arr,self.model['fit_parameter']['joint']['distance']['nNN'][n,:])
        #self.model['pdf']['joint'][1,:,n] = f_nNN*(1-self.model['p_same']['single']['fp_correlation'][n])

    ## obtain probability of being same neuron
    self.model['p_same']['joint'] = 1-self.model['pdf']['joint'][1,...]/np.nansum(self.model['pdf']['joint'],0)
    #if count_thr > 0:
      #self.model['p_same']['joint'] *= np.minimum(self.model[key_counts][...,0],count_thr)/count_thr
    #sp.ndimage.filters.gaussian_filter(self.model['p_same']['joint'],2,output=self.model['p_same']['joint'])

  #def get_p_same(self,d,fp):

    #f_total


  def set_functions(self):

    functions = {}
    ## define some possible fitting functions
    functions['lognorm'] = lambda x,sigma,mu : 1/(x*sigma*np.sqrt(2*np.pi))*np.exp(-(np.log(x)-mu)**2/(2*sigma**2))
    functions['lognorm_reverse'] = lambda x,sigma,mu : 1/((1-x)*sigma*np.sqrt(2*np.pi))*np.exp(-(np.log(1-x)-mu)**2/(2*sigma**2))
    functions['lognorm_shifted'] = lambda x,sigma,mu,s : 1/((x+s)*sigma*np.sqrt(2*np.pi))*np.exp(-(np.log(x+s)-mu)**2/(2*sigma**2))
    functions['lognorm_reverse_shifted'] = lambda x,sigma,mu,s : 1/((-x+s)*sigma*np.sqrt(2*np.pi))*np.exp(-(np.log(-x+s)-mu)**2/(2*sigma**2))
    functions['gauss'] = lambda x,sigma,mu : 1/np.sqrt(2*np.pi*sigma**2)*np.exp(-(x-mu)**2/(2*sigma**2))

    phi = lambda x : 1/np.sqrt(2*np.pi)*np.exp(-1/2*x**2)
    psi = lambda x : 1/2*(1 + sp.special.erf(x/np.sqrt(2)))
    functions['truncated_lognorm'] = lambda x,sigma,mu : 1/sigma * phi((x-mu)/sigma) / (psi((1-mu)/sigma) - psi((0-mu)/sigma))
    functions['truncated_lognorm_reverse'] = lambda x,sigma,mu : 1/sigma * phi((1-x-mu)/sigma) / (psi((1-mu)/sigma) - psi((0-mu)/sigma))

    functions['beta'] = lambda x,a,b : x**(a-1)*(1-x)**(b-1) / (math.gamma(a)*math.gamma(b)/math.gamma(a+b))
    functions['linear_sigmoid'] = lambda x,m,sig_slope,sig_center : m*x/(1+np.exp(-sig_slope*(x-sig_center)));

    ## set functions for model
    self.model['fit_function']['distance']['NN'] = functions['lognorm']
    self.model['fit_function']['distance']['nNN'] = functions['linear_sigmoid']
    self.model['fit_function']['distance']['nNN_joint'] = functions['gauss']
    self.model['fit_function']['distance']['all'] = lambda x,p,sigma,mu,m,sig_slope,sig_center : p*functions['lognorm'](x,sigma,mu) + (1-p)*functions['linear_sigmoid'](x,m,sig_slope,sig_center)

    self.model['fit_function']['fp_correlation']['NN'] = functions['lognorm_reverse']
    if self.para['model'] == 'new':
      self.model['fit_function']['fp_correlation']['nNN'] = functions['gauss']
      self.model['fit_function']['fp_correlation']['all'] = lambda x,p,sigma1,mu1,sigma2,mu2 : p*functions['lognorm_reverse'](x,sigma1,mu1) + (1-p)*functions['gauss'](x,sigma2,mu2)
    else:
      self.model['fit_function']['fp_correlation']['nNN'] = functions['beta']
      self.model['fit_function']['fp_correlation']['all'] = lambda x,p,sigma1,mu1,a,b : p*functions['lognorm_reverse'](x,sigma1,mu1) + (1-p)*functions['beta'](x,a,b)
    self.model['fit_function']['fp_correlation']['nNN_joint'] = functions['gauss']


  def plot_model(self,animate=False,sv=False,suffix=''):

    rc('font',size=10)
    rc('axes',labelsize=12)
    rc('xtick',labelsize=8)
    rc('ytick',labelsize=8)

    key_counts = 'counts' if self.para['model']=='new' else 'counts_old'
    nbins = self.model[key_counts].shape[0]

    d_arr = np.linspace(0,self.para['d_thr'],nbins+1)[1:]
    d_w = np.diff(d_arr)[0]

    fp_arr = np.linspace(0,1,nbins+1)[:-1]
    fp_w = np.diff(fp_arr)[0]
    X, Y = np.meshgrid(fp_arr, d_arr)

    mean_corr_NN, var_corr_NN = mean_of_trunc_lognorm(self.model['fit_parameter']['joint']['fp_correlation']['NN'][:,1],self.model['fit_parameter']['joint']['fp_correlation']['NN'][:,0],[0,1])
    mean_dist_NN, var_dist_NN = mean_of_trunc_lognorm(self.model['fit_parameter']['joint']['distance']['NN'][:,1],self.model['fit_parameter']['joint']['distance']['NN'][:,0],[0,1])

    if self.para['model'] == 'old':
      a = self.model['fit_parameter']['joint']['fp_correlation']['nNN'][:,0]
      b = self.model['fit_parameter']['joint']['fp_correlation']['nNN'][:,1]
      #mean_corr_nNN = a/(a+b)
      #var_corr_nNN = a*b/((a+b)**2*(a+b+1))
      mean_corr_nNN = b
      var_corr_nNN = a
    else:
      #mean_corr_nNN, var_corr_nNN = mean_of_trunc_lognorm(self.model['fit_parameter']['joint']['fp_correlation']['nNN'][:,1],self.model['fit_parameter']['joint']['fp_correlation']['nNN'][:,0],[0,1])
      mean_corr_nNN = self.model['fit_parameter']['joint']['fp_correlation']['nNN'][:,1]
      var_corr_nNN = self.model['fit_parameter']['joint']['fp_correlation']['nNN'][:,0]
    mean_corr_NN = 1-mean_corr_NN
    #mean_corr_nNN = 1-mean_corr_nNN

    fig = plt.figure(figsize=(7,4),dpi=150)
    ax_phase = plt.axes([0.3,0.13,0.2,0.4])
    add_number(fig,ax_phase,order=1,offset=[-250,200])
    #ax_phase.imshow(self.model[key_counts][:,:,0],extent=[0,1,0,self.para['d_thr']],aspect='auto',clim=[0,0.25*self.model[key_counts][:,:,0].max()],origin='lower')
    NN_ratio = self.model[key_counts][:,:,1]/self.model[key_counts][:,:,0]
    cmap = plt.cm.RdYlGn
    NN_ratio = cmap(NN_ratio)
    NN_ratio[...,-1] = np.minimum(self.model[key_counts][...,0]/100,1)

    im_ratio = ax_phase.imshow(NN_ratio,extent=[0,1,0,self.para['d_thr']],aspect='auto',clim=[0,0.5],origin='lower')
    nlev = 3
    col = (np.ones((nlev,3)).T*np.linspace(0,1,nlev)).T
    p_levels = ax_phase.contour(X,Y,self.model['p_same']['joint'],levels=[0.05,0.5,0.95],colors='b',linestyles=[':','--','-'])
    ax_phase.set_xlim([0,1])
    ax_phase.set_ylim([0,self.para['d_thr']])
    ax_phase.tick_params(axis='x',which='both',bottom=True,top=True,labelbottom=False,labeltop=True)
    ax_phase.tick_params(axis='y',which='both',left=True,right=True,labelright=False,labelleft=True)
    ax_phase.yaxis.set_label_position("right")
    #ax_phase.xaxis.tick_top()
    ax_phase.set_xlabel('correlation')
    ax_phase.xaxis.set_label_coords(0.5,-0.15)
    ax_phase.set_ylabel('distance')
    ax_phase.yaxis.set_label_coords(1.15,0.5)

    im_ratio.cmap = cmap
    if self.para['model'] == 'old':
      cbaxes = plt.axes([0.41, 0.47, 0.07, 0.03])
      #cbar.ax.set_xlim([0,0.5])
    else:
      cbaxes = plt.axes([0.32, 0.2, 0.07, 0.03])
    cbar = plt.colorbar(im_ratio,cax=cbaxes,orientation='horizontal')
    #cbar.ax.set_xlabel('NN ratio')
    cbar.ax.set_xticks([0,0.5])
    cbar.ax.set_xticklabels(['nNN','NN'])

    #cbar.ax.set_xticks(np.linspace(0,1,2))
    #cbar.ax.set_xticklabels(np.linspace(0,1,2))


    ax_dist = plt.axes([0.05,0.13,0.2,0.4])

    ax_dist.barh(d_arr,self.model[key_counts][...,0].sum(1).flat,self.para['d_thr']/nbins,facecolor='k',alpha=0.5,orientation='horizontal')
    ax_dist.barh(d_arr,self.model[key_counts][...,2].sum(1),d_w,facecolor='salmon',alpha=0.5)
    ax_dist.barh(d_arr,self.model[key_counts][...,1].sum(1),d_w,facecolor='lightgreen',alpha=0.5)
    ax_dist.invert_xaxis()
    #h_d_move = ax_dist.bar(d_arr,np.zeros(nbins),d_w,facecolor='k')

    model_distance_all = (fun_wrapper(self.model['fit_function']['distance']['NN'],d_arr,self.model['fit_parameter']['single']['distance']['NN'])*self.model[key_counts][...,1].sum() + fun_wrapper(self.model['fit_function']['distance']['nNN'],d_arr,self.model['fit_parameter']['single']['distance']['nNN'])*self.model[key_counts][...,2].sum())*d_w

    ax_dist.plot(fun_wrapper(self.model['fit_function']['distance']['all'],d_arr,self.model['fit_parameter']['single']['distance']['all'])*self.model[key_counts][...,0].sum()*d_w,d_arr,'k:')
    ax_dist.plot(model_distance_all,d_arr,'k')

    ax_dist.plot(fun_wrapper(self.model['fit_function']['distance']['NN'],d_arr,self.model['fit_parameter']['single']['distance']['all'][1:3])*self.model['fit_parameter']['single']['distance']['all'][0]*self.model[key_counts][...,0].sum()*d_w,d_arr,'g')
    ax_dist.plot(fun_wrapper(self.model['fit_function']['distance']['NN'],d_arr,self.model['fit_parameter']['single']['distance']['NN'])*self.model[key_counts][...,1].sum()*d_w,d_arr,'g:')

    ax_dist.plot(fun_wrapper(self.model['fit_function']['distance']['nNN'],d_arr,self.model['fit_parameter']['single']['distance']['all'][3:])*(1-self.model['fit_parameter']['single']['distance']['all'][0])*self.model[key_counts][...,0].sum()*d_w,d_arr,'r')
    ax_dist.plot(fun_wrapper(self.model['fit_function']['distance']['nNN'],d_arr,self.model['fit_parameter']['single']['distance']['nNN'])*self.model[key_counts][...,2].sum()*d_w,d_arr,'r',linestyle=':')
    ax_dist.set_ylim([0,self.para['d_thr']])
    ax_dist.set_xlabel('counts')
    ax_dist.spines['left'].set_visible(False)
    ax_dist.spines['top'].set_visible(False)
    ax_dist.tick_params(axis='y',which='both',left=False,right=True,labelright=False,labelleft=False)

    ax_corr = plt.axes([0.3,0.63,0.2,0.325])
    ax_corr.bar(fp_arr,self.model[key_counts][...,0].sum(0).flat,1/nbins,facecolor='k',alpha=0.5)
    ax_corr.bar(fp_arr,self.model[key_counts][...,2].sum(0),1/nbins,facecolor='salmon',alpha=0.5)
    ax_corr.bar(fp_arr,self.model[key_counts][...,1].sum(0),1/nbins,facecolor='lightgreen',alpha=0.5)

    f_NN = fun_wrapper(self.model['fit_function']['fp_correlation']['NN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['NN'])
    f_nNN = fun_wrapper(self.model['fit_function']['fp_correlation']['nNN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['nNN'])
    model_fp_correlation_all = (f_NN*self.model[key_counts][...,1].sum() + f_nNN*self.model[key_counts][...,2].sum())*fp_w
    #ax_corr.plot(fp_arr,fun_wrapper(self.model['fit_function']['fp_correlation']['all'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['all'])*self.model[key_counts][...,0].sum()*fp_w,'k')
    ax_corr.plot(fp_arr,model_fp_correlation_all,'k')

    #ax_corr.plot(fp_arr,fun_wrapper(self.model['fit_function']['fp_correlation']['NN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['all'][1:3])*self.model['fit_parameter']['single']['fp_correlation']['all'][0]*self.model[key_counts][...,0].sum()*fp_w,'g')
    ax_corr.plot(fp_arr,fun_wrapper(self.model['fit_function']['fp_correlation']['NN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['NN'])*self.model[key_counts][...,1].sum()*fp_w,'g')

    #ax_corr.plot(fp_arr,fun_wrapper(self.model['fit_function']['fp_correlation']['nNN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['all'][3:])*(1-self.model['fit_parameter']['single']['fp_correlation']['all'][0])*self.model[key_counts][...,0].sum()*fp_w,'r')
    ax_corr.plot(fp_arr,fun_wrapper(self.model['fit_function']['fp_correlation']['nNN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['nNN'])*self.model[key_counts][...,2].sum()*fp_w,'r')

    ax_corr.set_ylabel('counts')
    ax_corr.set_xlim([0,1])
    ax_corr.spines['right'].set_visible(False)
    ax_corr.spines['top'].set_visible(False)
    ax_corr.tick_params(axis='x',which='both',bottom=True,top=False,labelbottom=False,labeltop=False)

    #ax_parameter =
    p_steps, rates = self.RoC(100)

    ax_cum = plt.axes([0.675,0.7,0.3,0.225])
    add_number(fig,ax_cum,order=2)

    uncertain = {}
    idx_low = np.where(p_steps>0.05)[0][0]
    idx_high = np.where(p_steps<0.95)[0][-1]
    for key in rates['cumfrac'].keys():

      if (rates['cumfrac'][key][idx_low]>0.01) & (rates['cumfrac'][key][idx_high]<0.99):
        ax_cum.fill_between([rates['cumfrac'][key][idx_low],rates['cumfrac'][key][idx_high]],[0,0],[1,1],facecolor='y',alpha=0.5)
      uncertain[key] = (rates['cumfrac'][key][idx_high] - rates['cumfrac'][key][idx_low])#/(1-rates['cumfrac'][key][idx_high+1])

    ax_cum.plot([0,1],[0.05,0.05],'b',linestyle=':')
    ax_cum.plot([0.5,1],[0.95,0.95],'b',linestyle='-')

    ax_cum.plot(rates['cumfrac']['joint'],p_steps[:-1],'grey',label='Joint')
    # ax_cum.plot(rates['cumfrac']['distance'],p_steps[:-1],'k',label='Distance')
    if self.para['model']=='old':
      ax_cum.plot(rates['cumfrac']['fp_correlation'],p_steps[:-1],'lightgrey',label='Correlation')
    ax_cum.set_ylabel('$p_{same}$')
    ax_cum.set_xlabel('cumulative fraction')
    #ax_cum.legend(fontsize=10,frameon=False)
    ax_cum.spines['right'].set_visible(False)
    ax_cum.spines['top'].set_visible(False)

    ax_uncertain = plt.axes([0.75,0.825,0.05,0.1])
    # ax_uncertain.bar(2,uncertain['distance'],facecolor='k')
    ax_uncertain.bar(3,uncertain['joint'],facecolor='k')
    if self.para['model']=='old':
        ax_uncertain.bar(1,uncertain['fp_correlation'],facecolor='lightgrey')
        ax_uncertain.set_xlim([1.5,3.5])
        ax_uncertain.set_xticks(range(1,4))
        ax_uncertain.set_xticklabels(['Corr.','Dist.','Joint'],rotation=60,fontsize=10)
    else:
        ax_uncertain.set_xticks([])
        ax_uncertain.set_xlim([2.5,3.5])
        # ax_uncertain.set_xticklabels(['Dist.','Joint'],rotation=60,fontsize=10)
        ax_uncertain.set_xticklabels([])
    ax_uncertain.set_ylim([0,0.2])
    ax_uncertain.spines['right'].set_visible(False)
    ax_uncertain.spines['top'].set_visible(False)
    ax_uncertain.set_title('uncertain fraction',fontsize=10)

    #ax_rates = plt.axes([0.83,0.6,0.15,0.3])
    #ax_rates.plot(rates['fp']['joint'],p_steps[:-1],'r',label='false positive rate')
    #ax_rates.plot(rates['tp']['joint'],p_steps[:-1],'g',label='true positive rate')

    #ax_rates.plot(rates['fp']['distance'],p_steps[:-1],'r--')
    #ax_rates.plot(rates['tp']['distance'],p_steps[:-1],'g--')

    #ax_rates.plot(rates['fp']['fp_correlation'],p_steps[:-1],'r:')
    #ax_rates.plot(rates['tp']['fp_correlation'],p_steps[:-1],'g:')
    #ax_rates.legend()
    #ax_rates.set_xlabel('rate')
    #ax_rates.set_ylabel('$p_{same}$')

    idx = np.where(p_steps == 0.3)[0]

    ax_RoC = plt.axes([0.675,0.13,0.125,0.3])
    add_number(fig,ax_RoC,order=3)
    ax_RoC.plot(rates['fp']['joint'],rates['tp']['joint'],'k',label='Joint')
    # ax_RoC.plot(rates['fp']['distance'],rates['tp']['distance'],'k',label='Distance')
    if self.para['model']=='old':
      ax_RoC.plot(rates['fp']['fp_correlation'],rates['tp']['fp_correlation'],'lightgrey',label='Correlation')
    ax_RoC.plot(rates['fp']['joint'][idx],rates['tp']['joint'][idx],'kx')
    # ax_RoC.plot(rates['fp']['distance'][idx],rates['tp']['distance'][idx],'kx')
    if self.para['model']=='old':
      ax_RoC.plot(rates['fp']['fp_correlation'][idx],rates['tp']['fp_correlation'][idx],'kx')
    ax_RoC.set_ylabel('true positive')
    ax_RoC.set_xlabel('false positive')
    ax_RoC.spines['right'].set_visible(False)
    ax_RoC.spines['top'].set_visible(False)
    ax_RoC.set_xlim([0,0.1])
    ax_RoC.set_ylim([0.6,1])


    ax_fp = plt.axes([0.925,0.13,0.05,0.1])
    # ax_fp.bar(2,rates['fp']['distance'][idx],facecolor='k')
    ax_fp.bar(3,rates['fp']['joint'][idx],facecolor='k')
    ax_fp.set_xticks([])

    if self.para['model']=='old':
        ax_fp.bar(1,rates['fp']['fp_correlation'][idx],facecolor='lightgrey')
        ax_fp.set_xlim([1.5,3.5])
        ax_fp.set_xticks(range(1,4))
        ax_fp.set_xticklabels(['Corr.','Dist.','Joint'],rotation=60,fontsize=10)
    else:
        ax_fp.set_xticks([])
        ax_fp.set_xlim([2.5,3.5])
        # ax_fp.set_xticklabels(['Dist.','Joint'],rotation=60,fontsize=10)
        ax_fp.set_xticklabels([])

    ax_fp.set_ylim([0,0.05])
    ax_fp.spines['right'].set_visible(False)
    ax_fp.spines['top'].set_visible(False)
    ax_fp.set_ylabel('false pos.',fontsize=10)

    ax_tp = plt.axes([0.925,0.33,0.05,0.1])
    add_number(fig,ax_tp,order=4,offset=[-100,25])
    # ax_tp.bar(2,rates['tp']['distance'][idx],facecolor='k')
    ax_tp.bar(3,rates['tp']['joint'][idx],facecolor='k')
    ax_tp.set_xticks([])
    if self.para['model']=='old':
      ax_tp.bar(1,rates['tp']['fp_correlation'][idx],facecolor='lightgrey')
      ax_tp.set_xlim([1.5,3.5])
    else:
      ax_tp.set_xlim([2.5,3.5])
      ax_fp.set_xticklabels([])
    ax_tp.set_ylim([0.7,1])
    ax_tp.spines['right'].set_visible(False)
    ax_tp.spines['top'].set_visible(False)
    #ax_tp.set_ylabel('fraction',fontsize=10)
    ax_tp.set_ylabel('true pos.',fontsize=10)

    #plt.tight_layout()
    plt.show(block=False)
    if sv:
      ext = 'png'
      path = pathcat([self.para['pathMouse'],'Sheintuch_matching_%s%s.%s'%(self.para['model'],suffix,ext)])
      plt.savefig(path,format=ext,dpi=150)
    return
    #ax_cvc = plt.axes([0.65,0.1,0.2,0.4])
    #idx = self.session_data['fp_corr_max']>0
    #ax_cvc.scatter(self.session_data['fp_corr_max'][idx].toarray().flat,self.session_data['fp_corr'][idx].toarray().flat,c='k',marker='.')
    #ax_cvc.plot([0,1],[0,1],'r--')
    #ax_cvc.set_xlim([0,1])
    #ax_cvc.set_ylim([0,1])
    #ax_cvc.set_xlabel('shifted correlation')
    #ax_cvc.set_ylabel('unshifted correlation')

    #plt.show(block=False)
    #return

    ## plot fit parameters
    plt.figure()
    plt.subplot(221)
    plt.plot(fp_arr,mean_dist_NN,'g',label='lognorm $\mu$')
    plt.plot(fp_arr,self.model['fit_parameter']['joint']['distance']['nNN'][:,1],'r',label='gauss $\mu$')
    plt.title('distance models')
    #plt.plot(fp_arr,self.model['fit_parameter']['joint']['distance']['all'][:,2],'g--')
    #plt.plot(fp_arr,self.model['fit_parameter']['joint']['distance']['all'][:,5],'r--')
    plt.legend()

    plt.subplot(223)
    plt.plot(fp_arr,var_dist_NN,'g',label='lognorm $\sigma$')
    plt.plot(fp_arr,self.model['fit_parameter']['joint']['distance']['nNN'][:,0],'r',label='gauss $\sigma$')
    #plt.plot(fp_arr,self.model['fit_parameter']['joint']['distance']['nNN'][:,1],'r--',label='dist $\gamma$')
    plt.legend()

    plt.subplot(222)
    plt.plot(d_arr,mean_corr_NN,'g',label='lognorm $\mu$')#self.model['fit_parameter']['joint']['fp_correlation']['NN'][:,1],'g')#
    plt.plot(d_arr,self.model['fit_parameter']['joint']['fp_correlation']['nNN'][:,1],'r',label='gauss $\mu$')
    plt.title('correlation models')
    plt.legend()

    plt.subplot(224)
    plt.plot(d_arr,var_corr_NN,'g',label='lognorm $\sigma$')
    plt.plot(d_arr,self.model['fit_parameter']['joint']['fp_correlation']['nNN'][:,0],'r',label='gauss $\sigma$')
    plt.legend()
    plt.tight_layout()
    plt.show(block=False)

    #return
    fig = plt.figure()
    plt.subplot(322)
    plt.plot(fp_arr,self.model['fit_parameter']['joint']['distance']['NN'][:,1],'g')
    plt.plot(fp_arr,self.model['fit_parameter']['joint']['distance']['nNN'][:,1],'r')
    plt.xlim([0,1])
    plt.ylim([0,self.para['d_thr']])

    plt.subplot(321)
    plt.plot(d_arr,mean_corr_NN,'g')#self.model['fit_parameter']['joint']['fp_correlation']['NN'][:,1],'g')#
    plt.plot(d_arr,mean_corr_nNN,'r')#self.model['fit_parameter']['joint']['fp_correlation']['nNN'][:,1],'r')#
    plt.xlim([0,self.para['d_thr']])
    plt.ylim([0.5,1])


    plt.subplot(323)
    plt.bar(d_arr,self.model[key_counts][...,0].sum(1).flat,self.para['d_thr']/nbins,facecolor='k',alpha=0.5)
    plt.bar(d_arr,self.model[key_counts][...,2].sum(1),d_w,facecolor='r',alpha=0.5)
    plt.bar(d_arr,self.model[key_counts][...,1].sum(1),d_w,facecolor='g',alpha=0.5)
    h_d_move = plt.bar(d_arr,np.zeros(nbins),d_w,facecolor='k')

    model_distance_all = (fun_wrapper(self.model['fit_function']['distance']['NN'],d_arr,self.model['fit_parameter']['single']['distance']['NN'])*self.model[key_counts][...,1].sum() + fun_wrapper(self.model['fit_function']['distance']['nNN'],d_arr,self.model['fit_parameter']['single']['distance']['nNN'])*self.model[key_counts][...,2].sum())*d_w

    plt.plot(d_arr,fun_wrapper(self.model['fit_function']['distance']['all'],d_arr,self.model['fit_parameter']['single']['distance']['all'])*self.model[key_counts][...,0].sum()*d_w,'k')
    plt.plot(d_arr,model_distance_all,'k--')

    plt.plot(d_arr,fun_wrapper(self.model['fit_function']['distance']['NN'],d_arr,self.model['fit_parameter']['single']['distance']['all'][1:3])*self.model['fit_parameter']['single']['distance']['all'][0]*self.model[key_counts][...,0].sum()*d_w,'g')
    plt.plot(d_arr,fun_wrapper(self.model['fit_function']['distance']['NN'],d_arr,self.model['fit_parameter']['single']['distance']['NN'])*self.model[key_counts][...,1].sum()*d_w,'g--')

    plt.plot(d_arr,fun_wrapper(self.model['fit_function']['distance']['nNN'],d_arr,self.model['fit_parameter']['single']['distance']['all'][3:])*(1-self.model['fit_parameter']['single']['distance']['all'][0])*self.model[key_counts][...,0].sum()*d_w,'r')
    plt.plot(d_arr,fun_wrapper(self.model['fit_function']['distance']['nNN'],d_arr,self.model['fit_parameter']['single']['distance']['nNN'])*self.model[key_counts][...,2].sum()*d_w,'r--')
    plt.xlim([0,self.para['d_thr']])
    plt.xlabel('distance')

    plt.subplot(324)
    plt.bar(fp_arr,self.model[key_counts][...,0].sum(0).flat,1/nbins,facecolor='k',alpha=0.5)
    plt.bar(fp_arr,self.model[key_counts][...,2].sum(0),1/nbins,facecolor='r',alpha=0.5)
    plt.bar(fp_arr,self.model[key_counts][...,1].sum(0),1/nbins,facecolor='g',alpha=0.5)
    h_fp_move = plt.bar(fp_arr,np.zeros(nbins),1/nbins,facecolor='k')

    model_fp_correlation_all = (fun_wrapper(self.model['fit_function']['fp_correlation']['NN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['NN'])*self.model[key_counts][...,1].sum() + fun_wrapper(self.model['fit_function']['fp_correlation']['nNN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['nNN'])*self.model[key_counts][...,2].sum())*fp_w
    plt.plot(fp_arr,fun_wrapper(self.model['fit_function']['fp_correlation']['all'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['all'])*self.model[key_counts][...,0].sum()*fp_w,'k')
    plt.plot(fp_arr,model_fp_correlation_all,'k--')

    plt.plot(fp_arr,fun_wrapper(self.model['fit_function']['fp_correlation']['NN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['all'][1:3])*self.model['fit_parameter']['single']['fp_correlation']['all'][0]*self.model[key_counts][...,0].sum()*fp_w,'g')
    plt.plot(fp_arr,fun_wrapper(self.model['fit_function']['fp_correlation']['NN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['NN'])*self.model[key_counts][...,1].sum()*fp_w,'g--')

    plt.plot(fp_arr,fun_wrapper(self.model['fit_function']['fp_correlation']['nNN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['all'][3:])*(1-self.model['fit_parameter']['single']['fp_correlation']['all'][0])*self.model[key_counts][...,0].sum()*fp_w,'r')
    plt.plot(fp_arr,fun_wrapper(self.model['fit_function']['fp_correlation']['nNN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['nNN'])*self.model[key_counts][...,2].sum()*fp_w,'r--')

    plt.xlabel('correlation')
    plt.xlim([0,1])

    ax_d = plt.subplot(326)
    d_bar1 = ax_d.bar(d_arr,self.model[key_counts][:,0,0],self.para['d_thr']/nbins,facecolor='k',alpha=0.5)
    d_bar2 = ax_d.bar(d_arr,self.model[key_counts][:,0,2],self.para['d_thr']/nbins,facecolor='r',alpha=0.5)
    d_bar3 = ax_d.bar(d_arr,self.model[key_counts][:,0,1],self.para['d_thr']/nbins,facecolor='g',alpha=0.5)

    ### now, plot model stuff
    d_model_nNN, = ax_d.plot(d_arr,fun_wrapper(self.model['fit_function']['distance']['nNN_joint'],d_arr,self.model['fit_parameter']['joint']['distance']['nNN'][0,:]),'r')
    d_model_NN, = ax_d.plot(d_arr,fun_wrapper(self.model['fit_function']['distance']['NN'],d_arr,self.model['fit_parameter']['joint']['distance']['NN'][0,:]),'g')

    h_d = [d_bar1,d_bar3,d_bar2,h_d_move,d_model_NN,d_model_nNN]
    #h_d = d_bar1
    ax_d.set_xlabel('distance')
    ax_d.set_xlim([0,self.para['d_thr']])
    ax_d.set_ylim([0,self.model[key_counts][...,0].max()*1.1])


    ax_fp = plt.subplot(325)
    fp_bar1 = ax_fp.bar(fp_arr,self.model[key_counts][0,:,0],1/nbins,facecolor='k',alpha=0.5)
    fp_bar2 = ax_fp.bar(fp_arr,self.model[key_counts][0,:,2],1/nbins,facecolor='r',alpha=0.5)
    fp_bar3 = ax_fp.bar(fp_arr,self.model[key_counts][0,:,1],1/nbins,facecolor='g',alpha=0.5)

    ### now, plot model stuff
    fp_model_nNN, = ax_fp.plot(fp_arr,fun_wrapper(self.model['fit_function']['fp_correlation']['nNN_joint'],fp_arr,self.model['fit_parameter']['joint']['fp_correlation']['nNN'][0,:]),'r')
    fp_model_NN, = ax_fp.plot(fp_arr,fun_wrapper(self.model['fit_function']['fp_correlation']['NN'],fp_arr,self.model['fit_parameter']['joint']['fp_correlation']['NN'][0,:]),'g')


    h_fp = [fp_bar1,fp_bar3,fp_bar2,h_fp_move,fp_model_NN,fp_model_nNN]
    ax_fp.set_xlabel('corr')
    ax_fp.set_xlim([0,1])
    ax_fp.set_ylim([0,self.model[key_counts][...,0].max()*1.1])


    def update_distr(i,h_d,h_fp):

      n = i%self.model[key_counts].shape[0]
      for k in range(3):
        [h.set_height(dat) for h,dat in zip(h_d[k],self.model[key_counts][:,n,k])]
        [h.set_height(dat) for h,dat in zip(h_fp[k],self.model[key_counts][n,:,k])]

      d_move = np.zeros(self.model[key_counts].shape[0])
      fp_move = np.zeros(self.model[key_counts].shape[1])

      d_move[n] = self.model[key_counts][n,:,0].sum()
      fp_move[n] = self.model[key_counts][:,n,0].sum()

      [h.set_height(dat) for h,dat in zip(h_d[3],d_move)]
      [h.set_height(dat) for h,dat in zip(h_fp[3],fp_move)]

      self.model['p_same']['single']['distance'][n]*self.model[key_counts][n,:,0].sum()
      (1-self.model['p_same']['single']['distance'][n])*self.model[key_counts][n,:,0].sum()


      h_fp[4].set_ydata(fun_wrapper(self.model['fit_function']['fp_correlation']['NN'],fp_arr,self.model['fit_parameter']['joint']['fp_correlation']['NN'][n,:])*self.model['p_same']['single']['distance'][n]*self.model[key_counts][n,:,0].sum()*fp_w)
      h_fp[5].set_ydata(fun_wrapper(self.model['fit_function']['fp_correlation']['nNN_joint'],fp_arr,self.model['fit_parameter']['joint']['fp_correlation']['nNN'][n,:])*(1-self.model['p_same']['single']['distance'][n])*self.model[key_counts][n,:,0].sum()*fp_w)

      self.model['p_same']['single']['fp_correlation'][n]*self.model[key_counts][:,n,0].sum()
      (1-self.model['p_same']['single']['fp_correlation'][n])*self.model[key_counts][:,n,0].sum()

      h_d[4].set_ydata(fun_wrapper(self.model['fit_function']['distance']['NN'],d_arr,self.model['fit_parameter']['joint']['distance']['NN'][n,:])*self.model['p_same']['single']['fp_correlation'][n]*self.model[key_counts][:,n,0].sum()*d_w)

      h_d[5].set_ydata(fun_wrapper(self.model['fit_function']['distance']['nNN_joint'],d_arr,self.model['fit_parameter']['joint']['distance']['nNN'][n,:])*(1-self.model['p_same']['single']['fp_correlation'][n])*self.model[key_counts][:,n,0].sum()*d_w)
      #print(tuple(h_d[0]))
      return tuple(h_d[0]) + tuple(h_d[1]) + tuple(h_d[2]) + tuple(h_d[3]) + (h_d[4],) + (h_d[5],) + tuple(h_fp[0]) + tuple(h_fp[1]) + tuple(h_fp[2]) + tuple(h_fp[3]) + (h_fp[4],) + (h_fp[5],)

    plt.tight_layout()
    if animate or False:

      #Writer = animation.writers['ffmpeg']
      #writer = Writer(fps=15, metadata=dict(artist='Me'), bitrate=900)

      anim = animation.FuncAnimation(fig, update_distr, fargs=(h_d,h_fp),frames=nbins,interval=100, blit=True)
      svPath = pathcat([self.para['pathMouse'],'animation_single_models_%s.gif'%model])
      anim.save(svPath, writer='imagemagick',fps=15)#writer)
      print('animation saved at %s'%svPath)
      #anim
      plt.show()
    else:
      update_distr(20,h_d,h_fp)
      plt.show(block=False)
    #return


    counts1 = sp.ndimage.gaussian_filter(self.model['counts'][...,0],[1,1])
    counts2 = sp.ndimage.gaussian_filter(self.model['counts_same'],[1,1])

    counts1 /= counts1.sum(1)[-10:].sum()
    counts2 /= counts2.sum(1)[-10:].sum()

    counts_dif = counts1-counts2
    counts_dif[self.model['counts'][...,0]<2] = 0

    p_same = counts_dif/counts1
    p_same[counts1==0] = 0
    p_same = sp.ndimage.median_filter(p_same,[3,3],mode='nearest')
    self.f_same = sp.interpolate.RectBivariateSpline(d_arr,fp_arr,p_same,kx=1,ky=1)
    p_same = np.maximum(0,sp.ndimage.gaussian_filter(self.f_same(d_arr,fp_arr),[1,1]))

    #self.model['p_same']['joint'] = p_same
    plt.figure()
    X, Y = np.meshgrid(fp_arr, d_arr)

    ax = plt.subplot(221,projection='3d')
    ax.plot_surface(X,Y,counts1,alpha=0.5)
    ax.plot_surface(X,Y,counts2,alpha=0.5)

    ax = plt.subplot(222,projection='3d')
    ax.plot_surface(X,Y,counts_dif,cmap='jet')

    ax = plt.subplot(223,projection='3d')
    ax.plot_surface(X,Y,p_same,cmap='jet')

    ax = plt.subplot(224,projection='3d')
    ax.plot_surface(X,Y,self.model['p_same']['joint'],cmap='jet')

    plt.show(block=False)





    plt.figure()
    ax = plt.subplot(111,projection='3d')
    X, Y = np.meshgrid(fp_arr, d_arr)
    NN_ratio = self.model[key_counts][:,:,1]/self.model[key_counts][:,:,0]
    cmap = plt.cm.RdYlGn
    NN_ratio = cmap(NN_ratio)
    ax.plot_surface(X,Y,self.model[key_counts][:,:,0],facecolors=NN_ratio)
    ax.view_init(30,-120)
    ax.set_xlabel('footprint correlation',fontsize=14)
    ax.set_ylabel('distance',fontsize=14)
    ax.set_zlabel('# pairs',fontsize=14)
    plt.tight_layout()
    plt.show(block=False)

    if sv:
      ext = 'png'
      path = pathcat([self.para['pathMouse'],'Sheintuch_matching_phase_%s%s.%s'%(self.para['model'],suffix,ext)])
      plt.savefig(path,format=ext,dpi=300)

    return


    plt.figure()
    plt.subplot(221)
    plt.imshow(self.model[key_counts][:,:,0],extent=[0,1,0,self.para['d_thr']],aspect='auto',clim=[0,self.model[key_counts][:,:,0].max()],origin='lower')
    nlev = 3
    col = (np.ones((nlev,3)).T*np.linspace(0,1,nlev)).T
    p_levels = plt.contour(X,Y,self.model['p_same']['joint'],levels=[0.05,0.5,0.95],colors=col)
    #plt.colorbar(p_levels)
    #plt.imshow(self.model[key_counts][...,0],extent=[0,1,0,self.para['d_thr']],aspect='auto',origin='lower')
    plt.subplot(222)
    plt.imshow(self.model[key_counts][...,0],extent=[0,1,0,self.para['d_thr']],aspect='auto',origin='lower')
    plt.subplot(223)
    plt.imshow(self.model[key_counts][...,1],extent=[0,1,0,self.para['d_thr']],aspect='auto',origin='lower')
    plt.subplot(224)
    plt.imshow(self.model[key_counts][...,2],extent=[0,1,0,self.para['d_thr']],aspect='auto',origin='lower')
    plt.show(block=False)




    plt.figure()
    W_NN = self.model[key_counts][...,1].sum() / self.model[key_counts][...,0].sum()
    #W_NN = 0.5
    W_nNN = 1-W_NN
    plt.subplot(211)
    pdf_NN = fun_wrapper(self.model['fit_function']['fp_correlation']['NN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['NN'])*fp_w
    pdf_nNN = fun_wrapper(self.model['fit_function']['fp_correlation']['nNN'],fp_arr,self.model['fit_parameter']['single']['fp_correlation']['nNN'])*fp_w
    pdf_all = pdf_NN*W_NN+pdf_nNN*W_nNN

    plt.plot(fp_arr,pdf_NN*W_NN,'g')
    plt.plot(fp_arr,pdf_nNN*W_nNN,'r')
    plt.plot(fp_arr,pdf_all)

    plt.subplot(212)
    plt.plot(fp_arr,pdf_NN*W_NN/pdf_all,'k')
    plt.ylim([0,1])

    plt.show(block=False)



    X, Y = np.meshgrid(fp_arr, d_arr)

    fig,((ax1,ax2),(ax3,ax4)) = plt.subplots(2,2,figsize=(10,8),subplot_kw={'projection':'3d'})
    #ax = plt.subplot(221,projection='3d')
    prob = ax1.plot_surface(X,Y,self.model['pdf']['joint'][0,...],cmap='jet')
    prob.set_clim(0,6)
    ax1.set_xlabel('corr')
    ax1.set_ylabel('d')
    ax1.set_zlabel('model')
    #ax1 = plt.subplot(222,projection='3d')
    prob = ax2.plot_surface(X,Y,self.model['pdf']['joint'][1,...],cmap='jet')
    prob.set_clim(0,6)
    ax2.set_xlabel('corr')
    ax2.set_ylabel('d')
    ax2.set_zlabel('model')

    prob = ax3.plot_surface(X,Y,self.model['p_same']['joint'],cmap='jet')
    prob.set_clim(0,1)
    ax3.set_zlim([0,1])
    ax3.set_xlabel('corr')
    ax3.set_ylabel('d')
    ax3.set_zlabel('model')

    #ax = plt.subplot(224,projection='3d')
    prob = ax4.plot_surface(X,Y,self.model[key_counts][...,0],cmap='jet')
    #prob = ax.bar3d(X.flatten(),Y.flatten(),np.zeros((nbins,nbins)).flatten(),np.ones((nbins,nbins)).flatten()*fp_w,np.ones((nbins,nbins)).flatten()*d_w,self.model[key_counts][...,0].flatten(),cmap='jet')
    #prob.set_clim(0,1)
    ax4.set_xlabel('corr')
    ax4.set_ylabel('d')
    ax4.set_zlabel('occurence')
    plt.tight_layout()
    axes = [ax1,ax2,ax3,ax4]

    def rotate_view(i,axes,fixed_angle=30):
      for ax in axes:
        ax.view_init(fixed_angle,(i*2)%360)
      #return ax

    if animate:
      anim = animation.FuncAnimation(fig, rotate_view, fargs=(axes,30), frames=180, interval=100, blit=False)
      svPath = pathcat([self.para['pathMouse'],'animation_p_same.gif'])
      anim.save(svPath, writer='imagemagick',fps=15)#writer)
      print('animation saved at %s'%svPath)
      #anim
      #plt.show()
    else:
      rotate_view(100,axes,fixed_angle=30)
      plt.show(block=False)
    #anim.save('animation.mp4', writer=writer)
    #print('animation saved')

    print('proper weighting of bin counts')
    print('smoothing by gaussian')

  def RoC(self,steps):
    key_counts = 'counts' if self.para['model']=='new' else 'counts_old'
    p_steps = np.linspace(0,1,steps+1)

    rates = {'tp':      {},
             'tn':      {},
             'fp':      {},
             'fn':      {},
             'cumfrac': {}}

    for key in rates.keys():
      rates[key] = {'joint':np.zeros(steps),
                    'distance':np.zeros(steps),
                    'fp_correlation':np.zeros(steps)}

    nTotal = self.model[key_counts][...,0].sum()
    for i in range(steps):
      p = p_steps[i]

      for key in ['joint','distance','fp_correlation']:

        if key == 'joint':
          idxes_negative = self.model['p_same']['joint'] < p
          idxes_positive = self.model['p_same']['joint'] >= p

          tp = self.model[key_counts][idxes_positive,1].sum()
          tn = self.model[key_counts][idxes_negative,2].sum()
          fp = self.model[key_counts][idxes_positive,2].sum()
          fn = self.model[key_counts][idxes_negative,1].sum()

          rates['cumfrac']['joint'][i] = self.model[key_counts][idxes_negative,0].sum()/nTotal
        elif key == 'distance':
          idxes_negative = self.model['p_same']['single']['distance'] < p
          idxes_positive = self.model['p_same']['single']['distance'] >= p

          tp = self.model[key_counts][idxes_positive,:,1].sum()
          tn = self.model[key_counts][idxes_negative,:,2].sum()
          fp = self.model[key_counts][idxes_positive,:,2].sum()
          fn = self.model[key_counts][idxes_negative,:,1].sum()

          rates['cumfrac']['distance'][i] = self.model[key_counts][idxes_negative,:,0].sum()/nTotal
        else:
          idxes_negative = self.model['p_same']['single']['fp_correlation'] < p
          idxes_positive = self.model['p_same']['single']['fp_correlation'] >= p

          tp = self.model[key_counts][:,idxes_positive,1].sum()
          tn = self.model[key_counts][:,idxes_negative,2].sum()
          fp = self.model[key_counts][:,idxes_positive,2].sum()
          fn = self.model[key_counts][:,idxes_negative,1].sum()

          rates['cumfrac']['fp_correlation'][i] = self.model[key_counts][:,idxes_negative,0].sum()/nTotal

        rates['tp'][key][i] = tp/(fn+tp)
        rates['tn'][key][i] = tn/(fp+tn)
        rates['fp'][key][i] = fp/(fp+tn)
        rates['fn'][key][i] = fn/(fn+tp)

    return p_steps, rates

  def save_registration(self,suffix=''):

    idx = self.data['nA']>0
    results = {'assignments':self.assignments,
               'p_matched':self.p_matched,
               'p_same':self.data['p_same'],
               'nA':self.data['nA']}

    print(results['assignments'].shape)

    results['cm'] = np.zeros(results['assignments'].shape + (2,))

    for s in np.where(idx)[0]:
      idx_c = np.where(~np.isnan(results['assignments'][:,s]))[0]
      idx_n = results['assignments'][idx_c,s].astype('int')
      if s>0:
        results['p_same'][s] = sp.sparse.csr_matrix(results['p_same'][s])

      results['cm'][idx_c,s,:] = self.data['cm'][s][idx_n,:]


    pathSv = pathcat([self.para['pathMouse'],'matching/Sheintuch_registration_%s%s.pkl'%(os.path.splitext(self.para['fp_file'])[0],suffix)])
    pickleData(results,pathSv,'save')

  def load_registration(self,suffix=''):

    pathLd = pathcat([self.para['pathMouse'],'matching/Sheintuch_registration_%s%s.pkl'%(os.path.splitext(self.para['fp_file'])[0],suffix)])
    self.results = pickleData([],pathLd,'load')
    #try:
      #self.results['assignments'] = self.results['assignment']
    #except:
      #1

  def plot_registration(self,suffix='',dataSet='redetect',sv=False):

    rc('font',size=10)
    rc('axes',labelsize=12)
    rc('xtick',labelsize=8)
    rc('ytick',labelsize=8)

    pathLoad = pathcat([self.para['pathMouse'],'clusterStats_%s.pkl'%dataSet])
    ld = pickleData([],pathLoad,'load')
    if np.all(np.isnan(ld['SNR'])):
      idxes = np.ones(self.results['assignments'].shape,'bool')
    else:
      idxes = (ld['SNR']>2) & (ld['r_values']>0) & (ld['CNN']>0.3) & (ld['firingrate']>0)

    # ### plot occurence of neurons
    # colors = [(1,0,0,0),(1,0,0,1)]
    # RedAlpha = mcolors.LinearSegmentedColormap.from_list('RedAlpha',colors,N=2)
    # colors = [(0,0,0,0),(0,0,0,1)]
    # BlackAlpha = mcolors.LinearSegmentedColormap.from_list('BlackAlpha',colors,N=2)
    #
    #
    # plt.figure(figsize=(3,6))
    #
    # ### plot occurence of neurons
    # ax_oc = plt.subplot(111)
    # #ax_oc2 = ax_oc.twinx()
    # ax_oc.imshow((~np.isnan(self.results['assignments']))&idxes,cmap=BlackAlpha,aspect='auto')
    # #ax_oc2.imshow((~np.isnan(self.results['assignments']))&(~idxes),cmap=RedAlpha,aspect='auto')
    # #ax_oc.imshow(self.results['p_matched'],cmap='binary',aspect='auto')
    # ax_oc.set_xlabel('session')
    # ax_oc.set_ylabel('neuron ID')
    # plt.tight_layout()
    # plt.show(block=False)
    # ext = 'png'
    # path = pathcat([self.para['pathMouse'],'Figures/Sheintuch_registration_score_stats_raw_%s_%s_%s.%s'%(self.dataSet,self.para['model'],suffix,ext)])
    # plt.savefig(path,format=ext,dpi=300)


    # plt.figure(figsize=(7,3.5))

    # ax_oc = plt.axes([0.1,0.15,0.25,0.6])
    # ax_oc2 = ax_oc.twinx()
    # ax_oc.imshow((~np.isnan(self.results['assignments']))&idxes,cmap=BlackAlpha,aspect='auto')
    # ax_oc2.imshow((~np.isnan(self.results['assignments']))&(~idxes),cmap=RedAlpha,aspect='auto')
    # #ax_oc.imshow(self.results['p_matched'],cmap='binary',aspect='auto')
    # ax_oc.set_xlabel('session')
    # ax_oc.set_ylabel('neuron ID')
    #
    self.results['p_matched'][self.results['p_matched']==0] = np.NaN
    nC,nS = self.results['assignments'].shape
    ### plot point statistics
    if not ('match_2nd' in self.results.keys()):
      self.results['match_2nd'] = np.zeros((nC,nS))
      for s in tqdm(range(1,nS)):
        if s in self.results['p_same'].keys():
          idx_c = np.where(~np.isnan(self.results['assignments'][:,s]))[0]
          idx_c = idx_c[idx_c<self.results['p_same'][s].shape[0]]
          scores_now = self.results['p_same'][s].toarray()

          self.results['match_2nd'][idx_c,s] = [max(scores_now[c,np.where(scores_now[c,:]!=self.results['p_matched'][c,s])[0]]) for c in idx_c]
    #
    # ax = plt.axes([0.1,0.75,0.25,0.2])
    # ax.plot(np.linspace(0,nS,nS),(~np.isnan(self.results['assignments'])).sum(0),'ro',markersize=1)
    # ax.plot(np.linspace(0,nS,nS),((~np.isnan(self.results['assignments'])) & idxes).sum(0),'ko',markersize=1)
    # ax.set_xlim([0,nS])
    # ax.set_ylim([0,3500])
    # ax.set_xticks([])
    # ax.set_ylabel('# neurons')
    #
    # ax = plt.axes([0.35,0.15,0.1,0.6])
    # ax.plot(((~np.isnan(self.results['assignments'])) & idxes).sum(1),np.linspace(0,nC,nC),'ko',markersize=0.5)
    # ax.invert_yaxis()
    # ax.set_ylim([nC,0])
    # ax.set_yticks([])
    # ax.set_xlabel('occurence')
    #
    # ax = plt.axes([0.35,0.75,0.1,0.2])
    # ax.hist((~np.isnan(self.results['assignments'])).sum(1),np.linspace(0,nS,nS),color='r',cumulative=True,density=True,histtype='step')
    # ax.hist(((~np.isnan(self.results['assignments'])) & idxes).sum(1),np.linspace(0,nS,nS),color='k',alpha=0.5,cumulative=True,density=True,histtype='step')
    # ax.set_xticks([])
    # #ax.set_yticks([])
    # ax.yaxis.tick_right()
    # ax.yaxis.set_label_position("right")
    # ax.set_ylim([0,1])
    # #ax.set_ylabel('# neurons')
    # ax.spines['top'].set_visible(False)
    # #ax.spines['right'].set_visible(False)

    pm_thr = 0.5
    idx_pm = ((self.results['p_matched']-self.results['match_2nd'])>pm_thr) | (self.results['p_matched']>0.95)

    plt.figure(figsize=(7,1.5))
    ax_sc1 = plt.axes([0.1,0.3,0.35,0.65])

    ax = ax_sc1.twinx()
    ax.hist(self.results['match_2nd'][idxes].flat,np.linspace(0,1,51),facecolor='tab:red',alpha=0.3)
    #ax.invert_yaxis()
    ax.set_yticks([])
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)

    ax = ax_sc1.twiny()
    ax.hist(self.results['p_matched'][idxes].flat,np.linspace(0,1,51),facecolor='tab:blue',orientation='horizontal',alpha=0.3)
    ax.set_xticks([])
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)

    ax_sc1.plot(self.results['match_2nd'][idxes].flat,self.results['p_matched'][idxes].flat,'.',markeredgewidth=0,color='k',markersize=1)
    ax_sc1.plot([0,1],[0,1],'--',color='tab:red',lw=0.5)
    ax_sc1.plot([0,0.45],[0.5,0.95],'--',color='tab:orange',lw=1)
    ax_sc1.plot([0.45,1],[0.95,0.95],'--',color='tab:orange',lw=1)
    ax_sc1.set_ylabel('$p^{\\asterisk}$')
    ax_sc1.set_xlabel('max($p\\backslash p^{\\asterisk}$)')
    ax_sc1.spines['top'].set_visible(False)
    ax_sc1.spines['right'].set_visible(False)

    # match vs max
    # idxes &= idx_pm

    p_matched = np.copy(self.results['p_matched'])
    # p_matched[~idx_pm] = np.NaN
    # avg matchscore per cluster, min match score per cluster, ...
    ax_sc2 = plt.axes([0.6,0.3,0.35,0.65])
    #plt.hist(np.nanmean(self.results['p_matched'],1),np.linspace(0,1,51))
    ax = ax_sc2.twinx()
    ax.hist(np.nanmin(p_matched,1),np.linspace(0,1,51),facecolor='tab:red',alpha=0.3)
    ax.set_yticks([])
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)

    ax = ax_sc2.twiny()
    ax.hist(np.nanmean(p_matched,axis=1),np.linspace(0,1,51),facecolor='tab:blue',orientation='horizontal',alpha=0.3)
    ax.set_xticks([])
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)

    ax_sc2.plot(np.nanmin(p_matched,1),np.nanmean(p_matched,axis=1),'.',markeredgewidth=0,color='k',markersize=1)
    ax_sc2.set_xlabel('min($p^{\\asterisk}$)')
    ax_sc2.set_ylabel('$\left\langle p^{\\asterisk} \\right\\rangle$')
    ax_sc2.spines['top'].set_visible(False)
    ax_sc2.spines['right'].set_visible(False)

    ### plot positions of neurons
    plt.tight_layout()
    plt.show(block=False)

    if sv:
        ext = 'png'
        path = pathcat([self.para['pathMouse'],'Figures/Sheintuch_registration_score_stats_%s_%s_%s.%s'%(self.dataSet,self.para['model'],suffix,ext)])
        plt.savefig(path,format=ext,dpi=300)

  def save_model(self,suffix=''):

    pathSv = pathcat([self.para['pathMouse'],'matching/Sheintuch_model_%s%s.pkl'%(os.path.splitext(self.para['fp_file'])[0],suffix)])

    results = {}
    for key in ['p_same','fit_parameter','pdf','counts','counts_old','counts_same','counts_same_old']:
      results[key] = self.model[key]
    pickleData(results,pathSv,'save')


  def load_model(self,suffix=''):
    pathLd = pathcat([self.para['pathMouse'],'matching/Sheintuch_model_%s%s.pkl'%(os.path.splitext(self.para['fp_file'])[0],suffix)])
    results = pickleData([],pathLd,'load')
    for key in results.keys():
      self.model[key] = results[key]
    self.para['nbins'] = self.model['p_same']['joint'].shape[0]


def mean_of_trunc_lognorm(mu,sigma,trunc_loc):

  alpha = (trunc_loc[0]-mu)/sigma
  beta = (trunc_loc[1]-mu)/sigma

  phi = lambda x : 1/np.sqrt(2*np.pi)*np.exp(-1/2*x**2)
  psi = lambda x : 1/2*(1 + sp.special.erf(x/np.sqrt(2)))

  trunc_mean = mu + sigma * (phi(alpha) - phi(beta))/(psi(beta) - psi(alpha))
  trunc_var = np.sqrt(sigma**2 * (1 + (alpha*phi(alpha) - beta*phi(beta))/(psi(beta) - psi(alpha)) - ((phi(alpha) - phi(beta))/(psi(beta) - psi(alpha)))**2))

  return trunc_mean,trunc_var

def norm_nrg(a_):

  a = a_.copy()
  dims = a.shape
  a = a.reshape(-1, order='F')
  indx = np.argsort(a, axis=None)[::-1]
  cumEn = np.cumsum(a.flatten()[indx]**2)
  cumEn /= cumEn[-1]
  a = np.zeros(np.prod(dims))
  a[indx] = cumEn
  return a.reshape(dims, order='F')


def add_number(fig,ax,order=1,offset=None):

    # offset = [-175,50] if offset is None else offset
    offset = [-75,25] if offset is None else offset
    pos = fig.transFigure.transform(plt.get(ax,'position'))
    x = pos[0,0]+offset[0]
    y = pos[1,1]+offset[1]
    ax.text(x=x,y=y,s='%s)'%chr(96+order),ha='center',va='center',transform=None,weight='bold',fontsize=14)
