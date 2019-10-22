#This module is equivalent to the tools.py module, but includes dependency on molSimplify
#updates 8/2/19

import os
import glob
import copy
import numpy as np
import subprocess
import pandas as pd
import shutil
import time
import molSimplify.job_manager.tools as tools
from molSimplify.Classes.mol3D import mol3D
from molSimplify.Classes.ligand import ligand_breakdown

def read_run(outfile_PATH):
    #Evaluates all aspects of a run using the outfile and derivative files
    results = tools.read_outfile(outfile_PATH)
    
    optim_path = os.path.join(os.path.split(outfile_PATH)[0],'scr','optim.xyz')
    
    if os.path.isfile(optim_path):
        tools.extract_optimized_geo(optim_path)
        optimized_path = os.path.join(os.path.split(optim_path)[0],'optimized.xyz')
    
        mol = mol3D()
        mol.readfromxyz(optimized_path)
        geo_check_dict = mol.dict_oct_check_st
        #geo_check_dict['max_del_sig_angle'] = 90
        #geo_check_dict['oct_angle_devi_max'] = 35
        
        IsOct,flag_list,oct_check = mol.IsOct(dict_check = geo_check_dict,
                                              silent = True)
        
        if IsOct:
            IsOct = True
        else:
            IsOct = False
            
        results['Is_Oct'] = IsOct
        results['Flag_list'] = flag_list
        results['Oct_check_details'] = oct_check
    
    else:
        results['Is_Oct'] = None
        results['Flag_list'] = None
        results['Oct_check_details'] = None
    
    return results
        
def create_summary(directory='in place'):
    #Returns a pandas dataframe which summarizes all outfiles in the directory, defaults to cwd
            
    outfiles = tools.find('*.out',directory)
    outfiles = filter(tools.not_nohup,outfiles)
    results = map(read_run,outfiles)
    summary = pd.DataFrame(results)
    
    return summary

def apply_geo_check(job_outfile_path,geometry):
    
    if geometry: #The geometry variable is set to False if no geo check is requested for this job
        if geometry in ['Oct','oct','Octahedral','octahedral']:
            return read_run(job_outfile_path)['Is_Oct']
        else:
            print 'Geometry check request: '+configure_dict['geo_check']+' not recognized!'
            print 'Passing job: '+job_outfile_path+' without a geometry check!'
            return True
    else:
        print 'No geomery check requested for job: '+job_outfile_path
        print 'Passing job without a geometry check'
        return True
        
def get_metal_and_bonded_atoms(job_outfile,geometry = None):
    #given the path to the outfile of a job, returns a the metal atom index and a list of indices for the metal bonded atoms
    #indices are zero-indexed...Terachem uses 1 indexed lists
    
    xyz_path = job_outfile.rsplit('.',1)[0]+'.xyz'
    mol = mol3D()
    mol.readfromxyz(xyz_path)
    metal_index = mol.findMetal()[0]
    
    if geometry in ['Oct','oct','Octahedral','octahedral']:
        bonded_atom_indices = mol.getBondedAtomsOct(metal_index)
    else:
        print 'Warning, generic getBondedAtoms() used for: '+job_outfile+'. Check behavior'
        bonded_atom_indices = mol.getBondedAtoms(metal_index)
        
    return metal_index,bonded_atom_indices

def check_completeness(directory = 'in place', max_resub = 5):
    completeness = tools.check_completeness(directory,max_resub)
    
    #The check_completeness() function in tools doesn't check the geometries (because it's molSimplify dependent)
    #Apply the check here to finished and spin contaminated geometries, then update the completeness dictionary
    
    finished = completeness['Finished']
    spin_contaminated = completeness['Spin_contaminated']
    needs_resub = completeness['Resub']
    
    bad_geos = []
    new_finished = []
    new_spin_contaminated = []
    new_needs_resub = []
    for job in finished:
        goal_geo = tools.read_configure(directory,job)['geo_check']
        if apply_geo_check(job,goal_geo):
            new_finished.append(job)
        else:
            bad_geos.append(job)
    for job in spin_contaminated:
        goal_geo = tools.read_configure(directory,job)['geo_check']
        if apply_geo_check(job,goal_geo):
            new_spin_contaminated.append(job)
        else:
            bad_geos.append(job)
    for job in needs_resub:
        goal_geo = tools.read_configure(directory,job)['geo_check']
        if apply_geo_check(job,goal_geo):
            new_needs_resub.append(job)
        else:
            bad_geos.append(job)
    
    completeness['Finished'] = new_finished
    completeness['Spin_contaminated'] = new_spin_contaminated
    completeness['Resub'] = new_needs_resub
    completeness['Bad_geos'] = bad_geos
    return completeness
            
def prep_ligand_breakown(outfile_path):
    #Given a path to the outfile of a finished run, this preps the files for rigid ligand dissociation energies of all ligands
    #Returns a list of the PATH(s) to the jobscript(s) to start the rigid ligand calculations
        
    home = os.getcwd()
    outfile_path = tools.convert_to_absolute_path(outfile_path)
    
    results = tools.read_outfile(outfile_path)
    if not results['finished']:
        raise Exception('This calculation does not appear to be complete! Aborting...')
    
    
    charge,spinmult,solvent,run_type,levelshifta,levelshiftb,method,hfx,basis,convergence_thresholds,multibasis,constraints = tools.read_infile(outfile_path)
    charge = int(charge)
    spinmult = int(spinmult)    
    
    base = os.path.split(outfile_path)[0]
    name = os.path.split(outfile_path)[-1][:-4]
    
    breakdown_folder = os.path.join(base,name+'_dissociation')
    
    if os.path.isdir(breakdown_folder):
        return ['Ligand dissociation directory already exists']
    
    optimxyz = os.path.join(base,'scr','optim.xyz')
    tools.extract_optimized_geo(optimxyz)
    
    mol = mol3D()
    mol.readfromxyz(os.path.join(base,'scr','optimized.xyz'))
    
    ligand_idxs,_,_ = ligand_breakdown(mol,silent=True)
    
    ligand_syms = []
    for ii in ligand_idxs:
        ligand_syms.append([mol.getAtom(i).symbol() for i in ii])
        
    ligand_names = name_ligands(ligand_syms)
    
    if not os.path.isdir(breakdown_folder):
        os.mkdir(breakdown_folder)
    os.chdir(breakdown_folder)
    
    jobscripts = []
    for ligand in zip(ligand_names,ligand_idxs):
        
        #Assign charges to use during the breakdown for special cases...oxygen, hydroxide, peroxide, and acac
        #All other ligands are currently assigned charge 0
        ligand_charges = {'O1':-2, 'H1O1':-1, 'H1O2':-1, 'C5H7O2':-1}
        if ligand[0] in ligand_charges.keys():
            ligand_charge = ligand_charges[ligand[0]]
        else:
            ligand_charge = 0
        metal_charge = charge - ligand_charge
        
        #Assign spin, which always remains with the metal except for when an O2 leaves
        if spinmult == 1: #If the whole complex is restricted, it's components must be restricted as well
            ligand_spin,metal_spin = 1,1
        else:
            ligand_spinmults = {'O2':3}
            if ligand[0] in ligand_spinmults.keys():
                ligand_spin = ligand_spinmults[ligand[0]]
            else:
                ligand_spin = 1
            
            metal_spin = spinmult - ligand_spin + 1 #Derived from spinmult = (2S+1) where S=1/2 per electron
        
        
        #Create the necessary files for the metal complex single point
        local_name = name+'_rm_'+ligand[0]
        if os.path.isdir('rm_'+ligand[0]):
            pass
        else:
            os.mkdir('rm_'+ligand[0])
            os.chdir('rm_'+ligand[0])
            
            local_mol = mol3D()
            local_mol.copymol3D(mol)
            local_mol.deleteatoms(ligand[1])
            local_mol.writexyz(local_name+'.xyz')
            tools.write_input(local_name,metal_charge,metal_spin,run_type = 'energy', method = method, solvent = solvent,
                              levela = levelshifta, levelb = levelshiftb, thresholds = convergence_thresholds, hfx = hfx, basis = basis,
                              multibasis = multibasis)
            tools.write_jobscript(local_name,time_limit = '12:00:00', sleep = True)
            jobscripts.append(local_name+'.in')
            os.chdir('..')
        
        #Create the necessary files for the dissociated ligand single point
        local_name = name+'_kp_'+ligand[0]
        if os.path.isdir('kp_'+ligand[0]):
            pass
        else:
            os.mkdir('kp_'+ligand[0])
            os.chdir('kp_'+ligand[0])
            
            local_mol = mol3D()
            local_mol.copymol3D(mol)
            deletion_indices = list(set(range(local_mol.natoms))-set(ligand[1]))
            local_mol.deleteatoms(deletion_indices)
            local_mol.writexyz(local_name+'.xyz')
            tools.write_input(local_name,ligand_charge,ligand_spin,run_type = 'energy', method = method, solvent = solvent,
                              levela = levelshifta, levelb = levelshiftb, thresholds = convergence_thresholds, hfx = hfx, basis = basis,
                              multibasis = multibasis)
            tools.write_jobscript(local_name,time_limit = '12:00:00',sleep = True)
            jobscripts.append(local_name+'.in')
            os.chdir('..')
    os.chdir(home)
    
    return jobscripts
        
def name_ligands(nested_list):
    #takes a nested list of atom symbols and converts it to a list of unique chemical names based on the molecular formulas
    
    def convert_to_formula(list_of_atom_symbols):
        atom_types = list(set(list_of_atom_symbols))
        atom_types.sort()
        
        formula = ''
        for element in atom_types:
            counter = 0
            for atom in list_of_atom_symbols:
                if element == atom:
                    counter += 1
            formula += element
            formula += str(counter)
        return formula
    
    ligand_formulas = map(convert_to_formula,nested_list)
    
    duplicates = []
    for i in ligand_formulas:
        number_of_duplicates = 0
        for ii in ligand_formulas:
            if i == ii:
                number_of_duplicates += 1
        duplicates.append(number_of_duplicates)
    
    duplication_index = 1
    for counter,i in enumerate(duplicates):
        if i > 1:
            ligand_formulas[counter] = ligand_formulas[counter] + '_' + str(duplication_index)
            duplication_index += 1
            
    return ligand_formulas