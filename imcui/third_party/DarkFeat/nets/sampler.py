import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from .geom import rnd_sample, interpolate

class NghSampler2 (nn.Module):
    """ Similar to NghSampler, but doesnt warp the 2nd image.
    Distance to GT =>  0 ... pos_d ... neg_d ... ngh
    Pixel label    =>  + + + + + + 0 0 - - - - - - -
    
    Subsample on query side: if > 0, regular grid
                                < 0, random points 
    In both cases, the number of query points is = W*H/subq**2
    """
    def __init__(self, ngh, subq=1, subd=1, pos_d=0, neg_d=2, border=None,
                       maxpool_pos=True, subd_neg=0):
        nn.Module.__init__(self)
        assert 0 <= pos_d < neg_d <= (ngh if ngh else 99)
        self.ngh = ngh
        self.pos_d = pos_d
        self.neg_d = neg_d
        assert subd <= ngh or ngh == 0
        assert subq != 0
        self.sub_q = subq
        self.sub_d = subd
        self.sub_d_neg = subd_neg
        if border is None: border = ngh
        assert border >= ngh, 'border has to be larger than ngh'
        self.border = border
        self.maxpool_pos = maxpool_pos
        self.precompute_offsets()

    def precompute_offsets(self):
        pos_d2 = self.pos_d**2
        neg_d2 = self.neg_d**2
        rad2 = self.ngh**2
        rad = (self.ngh//self.sub_d) * self.ngh # make an integer multiple
        pos = []
        neg = []
        for j in range(-rad, rad+1, self.sub_d):
          for i in range(-rad, rad+1, self.sub_d):
            d2 = i*i + j*j
            if d2 <= pos_d2:
                pos.append( (i,j) )
            elif neg_d2 <= d2 <= rad2: 
                neg.append( (i,j) )

        self.register_buffer('pos_offsets', torch.LongTensor(pos).view(-1,2).t())
        self.register_buffer('neg_offsets', torch.LongTensor(neg).view(-1,2).t())

    def gen_grid(self, step, B, H, W, dev):
        b1 = torch.arange(B, device=dev)
        if step > 0:
            # regular grid
            x1 = torch.arange(self.border, W-self.border, step, device=dev)
            y1 = torch.arange(self.border, H-self.border, step, device=dev)
            H1, W1 = len(y1), len(x1)
            x1 = x1[None,None,:].expand(B,H1,W1).reshape(-1)
            y1 = y1[None,:,None].expand(B,H1,W1).reshape(-1)
            b1 = b1[:,None,None].expand(B,H1,W1).reshape(-1)
            shape = (B, H1, W1)
        else:
            # randomly spread
            n = (H - 2*self.border) * (W - 2*self.border) // step**2
            x1 = torch.randint(self.border, W-self.border, (n,), device=dev)
            y1 = torch.randint(self.border, H-self.border, (n,), device=dev)
            x1 = x1[None,:].expand(B,n).reshape(-1)
            y1 = y1[None,:].expand(B,n).reshape(-1)
            b1 = b1[:,None].expand(B,n).reshape(-1)
            shape = (B, n)
        return b1, y1, x1, shape

    def forward(self, feat0, feat1, conf0, conf1, pos0, pos1, B, H, W, N=2500):
        pscores_ls, nscores_ls, distractors_ls = [], [], []
        valid_feat0_ls = []
        valid_pos1_ls, valid_pos2_ls = [], []
        qconf_ls = []
        mask_ls = []

        for i in range(B):
            # positions in the first image
            tmp_mask = (pos0[i][:, 1] >= self.border) * (pos0[i][:, 1] < W-self.border) \
                * (pos0[i][:, 0] >= self.border) * (pos0[i][:, 0] < H-self.border)

            selected_pos0 = pos0[i][tmp_mask]
            selected_pos1 = pos1[i][tmp_mask]
            valid_pos0, valid_pos1 = rnd_sample([selected_pos0, selected_pos1], N)

            # sample features from first image
            valid_feat0 = interpolate(valid_pos0 / 4, feat0[i]) # [N, 128]
            valid_feat0 = F.normalize(valid_feat0, p=2, dim=-1) # [N, 128]
            qconf = interpolate(valid_pos0 / 4, conf0[i])

            # sample GT from second image
            mask = (valid_pos1[:, 1] >= 0) * (valid_pos1[:, 1] < W) \
                * (valid_pos1[:, 0] >= 0) * (valid_pos1[:, 0] < H)

            def clamp(xy):
                xy = xy
                torch.clamp(xy[0], 0, H-1, out=xy[0])
                torch.clamp(xy[1], 0, W-1, out=xy[1])
                return xy

            # compute positive scores
            valid_pos1p = clamp(valid_pos1.t()[:,None,:] + self.pos_offsets[:,:,None].to(valid_pos1.device)) # [2, 29, N]
            valid_pos1p = valid_pos1p.permute(1, 2, 0).reshape(-1, 2) # [29, N, 2] -> [29*N, 2]
            valid_feat1p = interpolate(valid_pos1p / 4, feat1[i]).reshape(self.pos_offsets.shape[-1], -1, 128) # [29, N, 128]
            valid_feat1p = F.normalize(valid_feat1p, p=2, dim=-1) # [29, N, 128]

            pscores = (valid_feat0[None,:,:] * valid_feat1p).sum(dim=-1).t() # [N, 29]
            pscores, pos = pscores.max(dim=1, keepdim=True)
            sel = clamp(valid_pos1.t() + self.pos_offsets[:,pos.view(-1)].to(valid_pos1.device))
            qconf = (qconf + interpolate(sel.t() / 4, conf1[i]))/2

            # compute negative scores
            valid_pos1n = clamp(valid_pos1.t()[:,None,:] + self.neg_offsets[:,:,None].to(valid_pos1.device)) # [2, 29, N]
            valid_pos1n = valid_pos1n.permute(1, 2, 0).reshape(-1, 2) # [29, N, 2] -> [29*N, 2]
            valid_feat1n = interpolate(valid_pos1n / 4, feat1[i]).reshape(self.neg_offsets.shape[-1], -1, 128) # [29, N, 128]
            valid_feat1n = F.normalize(valid_feat1n, p=2, dim=-1) # [29, N, 128]
            nscores = (valid_feat0[None,:,:] * valid_feat1n).sum(dim=-1).t() # [N, 29]

            if self.sub_d_neg:
                valid_pos2 = rnd_sample([selected_pos1], N)[0]
                distractors = interpolate(valid_pos2 / 4, feat1[i])
                distractors = F.normalize(distractors, p=2, dim=-1)

            pscores_ls.append(pscores)
            nscores_ls.append(nscores)
            distractors_ls.append(distractors)
            valid_feat0_ls.append(valid_feat0)
            valid_pos1_ls.append(valid_pos1)
            valid_pos2_ls.append(valid_pos2)
            qconf_ls.append(qconf)
            mask_ls.append(mask)

        N = np.min([len(i) for i in qconf_ls])

        # merge batches
        qconf = torch.stack([i[:N] for i in qconf_ls], dim=0).squeeze(-1)
        mask = torch.stack([i[:N] for i in mask_ls], dim=0)
        pscores = torch.cat([i[:N] for i in pscores_ls], dim=0)
        nscores = torch.cat([i[:N] for i in nscores_ls], dim=0)
        distractors = torch.cat([i[:N] for i in distractors_ls], dim=0)
        valid_feat0 = torch.cat([i[:N] for i in valid_feat0_ls], dim=0)
        valid_pos1 = torch.cat([i[:N] for i in valid_pos1_ls], dim=0)
        valid_pos2 = torch.cat([i[:N] for i in valid_pos2_ls], dim=0)

        dscores = torch.matmul(valid_feat0, distractors.t())
        dis2 = (valid_pos2[:, 1] - valid_pos1[:, 1][:,None])**2 + (valid_pos2[:, 0] - valid_pos1[:, 0][:,None])**2
        b = torch.arange(B, device=dscores.device)[:,None].expand(B, N).reshape(-1)
        dis2 += (b != b[:,None]).long() * self.neg_d**2
        dscores[dis2 < self.neg_d**2] = 0
        scores = torch.cat((pscores, nscores, dscores), dim=1)
        
        gt = scores.new_zeros(scores.shape, dtype=torch.uint8)
        gt[:, :pscores.shape[1]] = 1

        return scores, gt, mask, qconf
