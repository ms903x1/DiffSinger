from utils.hparams import hparams
import torch
from torch.nn import functional as F

class Batch2Loss:
    '''
        pipeline: batch -> insert1 -> module1 -> insert2 -> module2 -> insert3 -> module3 -> insert4 -> module4 -> post -> loss
    '''

    @staticmethod
    def insert1(pitch_midi, midi_dur, is_slur, # variables
                midi_embed, midi_dur_layer, is_slur_embed): # modules
        '''
            add embeddings for midi, midi_dur, slur
        '''
        midi_embedding = midi_embed(pitch_midi)
        midi_dur_embedding, slur_embedding = 0, 0
        if midi_dur is not None:
            midi_dur_embedding = midi_dur_layer(midi_dur[:, :, None])  # [B, T, 1] -> [B, T, H]
        if is_slur is not None:
            slur_embedding = is_slur_embed(is_slur)
        return midi_embedding, midi_dur_embedding, slur_embedding

    @staticmethod
    def module1(fs2_encoder, # modules
                txt_tokens, midi_embedding, midi_dur_embedding, slur_embedding): # variables
        '''
            get *encoder_out* == fs2_encoder(*txt_tokens*, some embeddings)
        '''
        encoder_out = fs2_encoder(txt_tokens, midi_embedding, midi_dur_embedding, slur_embedding)
        return encoder_out

    @staticmethod
    def insert2(encoder_out, spk_embed_id, spk_embed_dur_id, spk_embed_f0_id, src_nonpadding, # variables
                spk_embed_proj): # modules
        '''
            1. add embeddings for spk, spk_dur, spk_f0
            2. get *dur_inp* ~= *encoder_out* + *spk_embed_dur*
        '''
        # add ref style embed
        # Not implemented
        # variance encoder
        var_embed = 0

        # encoder_out_dur denotes encoder outputs for duration predictor
        # in speech adaptation, duration predictor use old speaker embedding
        if hparams['use_spk_embed']:
            spk_embed_dur = spk_embed_f0 = spk_embed = spk_embed_proj(spk_embed_id)[:, None, :]
        elif hparams['use_spk_id']:
            if spk_embed_dur_id is None:
                spk_embed_dur_id = spk_embed_id
            if spk_embed_f0_id is None:
                spk_embed_f0_id = spk_embed_id
            spk_embed = spk_embed_proj(spk_embed_id)[:, None, :]
            spk_embed_dur = spk_embed_f0 = spk_embed
            if hparams['use_split_spk_id']:
                spk_embed_dur = spk_embed_dur(spk_embed_dur_id)[:, None, :]
                spk_embed_f0 = spk_embed_f0(spk_embed_f0_id)[:, None, :]
        else:
            spk_embed_dur = spk_embed_f0 = spk_embed = 0

        # add dur
        dur_inp = (encoder_out + var_embed + spk_embed_dur) * src_nonpadding # src_nonpadding = (txt_tokens > 0).float()[:, :, None]
        return var_embed, spk_embed, spk_embed_dur, spk_embed_f0, dur_inp

    @staticmethod
    def module2(dur_predictor, length_regulator, # modules
                dur_input, mel2ph, txt_tokens, all_vowel_tokens, ret, midi_dur = None): # variables
        '''
            1. get *dur* ~= dur_predictor(*dur_inp*)
            2. (mel2ph is None): get *mel2ph* ~= length_regulater(*dur*)
        ''' 
        src_padding = (txt_tokens == 0)
        dur_input = dur_input.detach() + hparams['predictor_grad'] * (dur_input - dur_input.detach())
        
        if mel2ph is None:
            dur, xs = dur_predictor.inference(dur_input, src_padding)
            ret['dur'] = xs
            dur = xs.squeeze(-1).exp() - 1.0
            for i in range(len(dur)):
                for j in range(len(dur[i])):
                    if txt_tokens[i,j] in all_vowel_tokens:
                        if j < len(dur[i])-1 and txt_tokens[i,j+1] not in all_vowel_tokens:
                            dur[i,j] = midi_dur[i,j] - dur[i,j+1]
                            if dur[i,j] < 0:
                                dur[i,j] = 0
                                dur[i,j+1] = midi_dur[i,j]
                        else:
                            dur[i,j]=midi_dur[i,j]      
            dur[:,0] = dur[:,0] + 0.5
            dur_acc = F.pad(torch.round(torch.cumsum(dur, axis=1)), (1,0))
            dur = torch.clamp(dur_acc[:,1:]-dur_acc[:,:-1], min=0).long()
            ret['dur_choice'] = dur
            mel2ph = length_regulator(dur, src_padding).detach()
        else:
            ret['dur'] = dur_predictor(dur_input, src_padding)
        ret['mel2ph'] = mel2ph

        return mel2ph
    
    @staticmethod
    def insert3(encoder_out, mel2ph, var_embed, spk_embed_f0, tgt_nonpadding): # variables
        '''
            1. get *decoder_inp* ~= convert_ph_to_mel(*encoder_out*, *mel2ph*)
            2. get *pitch_inp* ~= *decoder_inp* + *spk_embed_f0*
        '''
        decoder_inp = F.pad(encoder_out, [0, 0, 1, 0])
        mel2ph_ = mel2ph[..., None].repeat([1, 1, encoder_out.shape[-1]])
        decoder_inp = torch.gather(decoder_inp, 1, mel2ph_)  # [B, T, H]

        # add pitch and energy embed
        pitch_inp = (decoder_inp + var_embed + spk_embed_f0) * tgt_nonpadding # tgt_nonpadding = (mel2ph > 0).float()[:, :, None]
        return pitch_inp

    @staticmethod
    def module3(pitch_predictor, energy_predictor, # modules
                ): # variables
        '''
            get *pitch_pred* and *energy_pred* using pitch_predictor and energy_predictor
        '''
        def add_pitch():
            pass
        def add_energy():
            pass
        raise NotImplementedError
    
    @staticmethod
    def insert4():
        '''
            add *spk_embed* to *ret['decoder_inp']*
        '''
        raise NotImplementedError

    @staticmethod
    def module4(diff_main_loss, # modules
                norm_spec, decoder_inp_t, ret, K_step, batch_size, device): # variables
        '''
            calc diffusion main loss, using spec as input and decoder_inp as condition.
            
            Args:
                norm_spec: (normalized) spec
                decoder_inp_t: (transposed) decoder_inp
            Returns:
                ret['diff_loss']
        '''
        t = torch.randint(0, K_step, (batch_size,), device=device).long()
        norm_spec = norm_spec.transpose(1, 2)[:, None, :, :]  # [B, 1, M, T]
        ret['diff_loss'] = diff_main_loss(norm_spec, t, cond=decoder_inp_t)
        # nonpadding = (mel2ph != 0).float()
        # ret['diff_loss'] = self.p_losses(x, t, cond, nonpadding=nonpadding)
    
    @staticmethod
    def post():
        '''
            calculate other losses: dur loss, pitch loss, energy loss
        '''
        pass