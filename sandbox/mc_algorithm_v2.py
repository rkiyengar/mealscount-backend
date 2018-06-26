
# coding: utf-8

# ## MealsCount Algorithm (v2)
#   

import os
import sys
import pandas as pd
import numpy as np
import json
import time
from datetime import datetime
import abc

import backend_utils as bu
import config_parser as cp

class mcAlgorithm(metaclass=abc.ABCMeta):
    """
    Base class for the MealsCount Algorithm. 
    """
            
    def __init__(self):                        
        pass    
    
    @abc.abstractmethod
    def version(self):
        pass
    
    @abc.abstractmethod
    def run(self,data,cfg):
        pass
    
    @abc.abstractmethod
    def get_school_groups(self,data):
        pass

class CEPSchoolGroupGenerator:
    """
    Class to encapsulate data and operations for grouping schools.
    """      
    __strategy = None
    
    def __init__(self,cfg,strategy=None):   
        if not (strategy):
            raise ValueError("ERROR: Invalid strategy")
        self.__strategy = strategy
        self.__config = cfg
            
    def get_groups(self,school_data):       
        
        json_data = {}
        
        if not (self.__strategy):
            raise ValueError("ERROR: Invalid strategy")
        
        try:
            algo = self.__strategy
            if algo.run(school_data,self.__config):
                json_data = algo.get_school_groups(school_data)                  
            else:
                s = "ERROR: Failed to generate school groups"
                print(s)
            return json_data
        except Exception as e:            
            raise e            

#
# Function wrangle the school district input data to the necessary form to 
# generate groupings of schools based on ISP
#
def prepare_data(df):
    
    # remove aggregated records
    df = df[df['school_name']!='total']
    
    # sum cols for homeless, migrant and foster students
    df = df.assign(non_direct_cert=(df['foster'] + df['homeless'] + df['migrant']))
    
    # compute total eligible and isp
    total_eligible = (df['foster'] + df['homeless'] + df['migrant'] + df['direct_cert'])
    isp = (total_eligible/df['total_enrolled']) * 100
    df = df.assign(total_eligible=total_eligible)
    df = df.assign(isp=isp)
    df.loc[:,'isp'] = np.around(df['isp'].astype(np.double),2)    
        
    KEEP_COLS = ['school_code','total_enrolled','direct_cert','non_direct_cert','total_eligible','isp']

    # remove cols not needed for further analysis
    drop_cols = [s for s in df.columns.tolist() if s not in set(KEEP_COLS)]
    df.drop(drop_cols,axis=1,inplace=True)
    
    # sort by isp
    df.sort_values('isp',ascending=False,inplace=True)
    df.reset_index(inplace=True)
    df.drop('index',axis=1,inplace=True)
    
    # compute cumulative isp
    cum_isp = np.around((df['total_eligible'].cumsum()/df['total_enrolled'].cumsum()).astype(np.double)*100,2)
    df = df.assign(cum_isp=cum_isp)
    
    return df

#
# Function to generate summary data for the specified group of schools
#
def summarize_group(group_df,cfg):
    
        # compute total eligible and total enrolled students across all schools in the group
        summary = group_df[['total_enrolled','direct_cert','non_direct_cert','total_eligible']].aggregate(['sum'])        
        # compute the group's ISP
        summary = summary.assign(grp_isp=round((summary['total_eligible']/summary['total_enrolled'])*100,2))            
        # count the number of schools in the group
        summary = summary.assign(size=group_df.shape[0])
        # compute the % of meals covered at the free and paid rate for the group's ISP
        grp_isp = summary.loc['sum','grp_isp']
        free_rate = round(grp_isp * 1.6,2) if grp_isp >= (cfg.min_cep_thold_pct()*100) else 0.0
        free_rate = 100. if free_rate > 100. else free_rate
        summary = summary.assign(free_rate=free_rate)
        paid_rate = (100.0 - free_rate)
        summary = summary.assign(paid_rate=paid_rate)
        
        return summary

#
# Function to select schools to add, from among all schools not already in the destination group (df), 
# to the destination group (whose summary is provided as input) based on the impact each school has on the 
# destination group's ISP. Target ISP specifies the desired ISP at which to maintain the destination group
#
def select_by_isp_impact(df,dst_group_summary,target_isp):
    
    schools_to_add = pd.DataFrame()
    
    dst_grp_total_enrolled = dst_group_summary['total_enrolled']
    dst_grp_total_eligible = dst_group_summary['total_eligible']

    new_total_enrolled = df['total_enrolled'] + dst_grp_total_enrolled
    new_isp = np.around((((df['total_eligible'] + dst_grp_total_eligible)/new_total_enrolled)*100).astype(np.double),2)        
    
    tmp_df = pd.DataFrame({'new_isp':new_isp})
    
    # select all schools whose ISP impact is small enough to not bring down the new ISP 
    # to under the target ISP
    idx = tmp_df[tmp_df['new_isp'] >= target_isp].index
    if len(idx) > 0:
        schools_to_add = df.loc[idx,:]
        
    return schools_to_add

#
# Function to take in school data and group them based on the ISP_WIDTH
#
def groupby_isp_width(df,cfg):
    
    min_cep_thold = (cfg.min_cep_thold_pct()*100)
    isp_width = cfg.isp_width()
    
    # recalculate cumulative-isp
    cum_isp=np.around((df['total_eligible'].cumsum()/df['total_enrolled'].cumsum()).astype(np.double)*100,2)
    df = df.assign(cum_isp=cum_isp)

    top_isp = df.iloc[0]['isp']
    
    # if the top ISP is less than that needed for CEP eligibility 
    # we have nothing more to do
    if top_isp < min_cep_thold:
        return None
    
    # determine the next cut-off point
    isp_thold = (top_isp - isp_width) if (top_isp-isp_width) >= min_cep_thold else min_cep_thold
   
    # group schools at the cut-off point
    # note that this will generate exactly 2 groups: one of length ISP_WIDTH and the other containing 
    # the rest of the schools     
    groups = df.groupby(pd.cut(df['cum_isp'], [0.,isp_thold,top_isp]))    
    
    return groups    

#
# Function that implements a strategy to group schools with ISPs lower than that needed for 
# 100% CEP funding.
#
def group_schools_lo_isp(df,cfg):
          
    school_groups = []
    school_group_summaries = []    
    
    top_isp = df.iloc[0]['isp']
    
    # exit the loop if the highest ISP from among the remaining schools (which are sorted by ISP)
    # is lower than that needed for CEP eligibility; we have nothing more to do
    
    while top_isp >= (cfg.min_cep_thold_pct()*100):
    
        # get the next isp_width group that still qualifies for CEP
        groups = groupby_isp_width(df,cfg)    
    
        if (groups != None):
            
            ivals = pd.DataFrame(groups.size()).index.tolist()
            
            # get the last group: this is the group of isp_width
            group_df = groups.get_group(ivals[-1]) 
            summary_df = summarize_group(group_df,cfg)
            
            # trim the school data to remove this group
            df.drop(group_df.index.tolist(),axis=0,inplace=True)                
            # from among remaining schools see if any qualify based on isp impact
            schools_to_add = select_by_isp_impact(df,summary_df,(cfg.max_cep_thold_pct()*100))
    
            if schools_to_add.shape[0] > 0:
                group_df = pd.concat([group_df, schools_to_add],axis=0)            
                df.drop(schools_to_add.index.tolist(),axis=0,inplace=True)        
            
            school_groups.append(group_df)
            
            summary_df = summarize_group(group_df,cfg)   
            school_group_summaries.append(summary_df)            
            
            # get the top isp for the remaining schools
            top_isp = df.iloc[0]['isp']            

    # at this point all remaining schools are ineligible for CEP 
    # pass them along as a group of their own
    cum_isp = np.around((df['total_eligible'].cumsum()/df['total_enrolled'].cumsum()).astype(np.double)*100,2)
    df = df.assign(cum_isp=cum_isp)        
    school_groups.append(df)
    
    summary_df = summarize_group(df,cfg)   
    school_group_summaries.append(summary_df)
    
    return school_groups,school_group_summaries

#
# Function that implements a strategy to group schools with ISPs higher than (or equal to) 
# that needed for 100% CEP funding.
#
def group_schools_hi_isp(df,cfg):
    
    school_groups = []
    school_group_summaries = []
    
    # group the data by cumulative ISP such that all schools with 
    # max CEP threshold and higher are part of a single group; the 
    # rest of the schools are in a second group
    
    bins = [0.,cfg.max_cep_thold_pct()*100,100.]
    
    groups = df.groupby(pd.cut(df['cum_isp'], bins))
    ivals = groups.size().index.tolist()
    
    group_df = groups.get_group(ivals[-1]).apply(list).apply(pd.Series)    
    summary_df = summarize_group(group_df,cfg)
    
    df.drop(group_df.index.tolist(),axis=0,inplace=True)        
    # from among remaining schools see if any qualify based on isp impact
    schools_to_add = select_by_isp_impact(df,summary_df,(cfg.max_cep_thold_pct()*100))
    
    if schools_to_add.shape[0] > 0:
        group_df = pd.concat([group_df, schools_to_add],axis=0)
        df.drop(schools_to_add.index.tolist(),axis=0,inplace=True)        
        
    school_groups.append(group_df)
    
    summary_df = summarize_group(group_df,cfg)
    school_group_summaries.append(summary_df)
    
    return school_groups,school_group_summaries


def show_results(groups,summaries):    
    
    n = len(groups)
    
    for i in range(n):
        print('GRP {}'.format(i))
        print(summaries[i])        
        print(groups[i])        
        
    return

#
# Function to prepare school group and summary data in JSON format
#
def prepare_results(groups,summaries,cfg,metadata):
    
    json_result = {}
    n = len(groups)
    
    json_result['lea'] = metadata['lea']
    json_result['academic_year'] = metadata['academic_year']
    json_result['timestamp'] = datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')    
    
    groups_dl = []
    for i in range(n):        
        
        g = summaries[i]
        
        eligibility = 'yes' if g.loc['sum','grp_isp'] >= (cfg.min_cep_thold_pct()*100) else 'no'                
        schools = groups[i].loc[:,'school_code'].values.tolist()
        
        # json does not serialize numpy types. convert them to native ones below
        
        g_json = {"group": i, 
                  "eligible_for_cep": eligibility, 
                  "total_enrolled": int(g.loc['sum','total_enrolled']),
                  "direct_cert": int(g.loc['sum','direct_cert']),
                  "non_direct_cert": int(g.loc['sum','non_direct_cert']),
                  "total_eligible": int(g.loc['sum','total_eligible']),
                  "group_isp": round(float(g.loc['sum','grp_isp']),2),
                  "group_size": int(g.loc['sum','size']),
                  "schools": schools}
        
        groups_dl.append(g_json)
        
    json_result['school_groups'] = {"num_groups": n, "group_summaries": groups_dl }
    json_result['mealscount_config_version'] = cfg.version()
    json_result['model_params'] = {'model_variant': cfg.model_variant(), 'isp_width': cfg.isp_width()}
    
    #print(json_result)
    
    return json_result

#
# Function to implement variant V2 of the algorithm
#
def runAlgorithmV2(self,data,cfg):
    
    status = True   
    
    md = data.metadata()
    df = data.to_frame()
    
    df = prepare_data(df)
    
    g1,s1 = group_schools_hi_isp(df,cfg)          
    g2,s2 = group_schools_lo_isp(df,cfg)
        
    self.school_groups = prepare_results(g1+g2,s1+s2,cfg,md)      
    
    # uncomment below for debugging
    # show_results(g1+g2,s1+s2)
    
    return status

class mcAlgorithmV2(mcAlgorithm):
    """
    Implementation of the MealsCount Algorithm variant V2
    """
            
    def __init__(self):                
        self.school_groups = {}
    
    def version(self):
        return "v2"        
    
    def run(self,data,cfg):
        status = self.__run(data,cfg)    
        return status
    
    def get_school_groups(self,data):        
        return self.school_groups
    
    __run = runAlgorithmV2

#
# MAIN
#
def main():

	CWD = os.getcwd()

	DATADIR = "data"
	DATAFILE = "calpads_sample_data.xlsx"

	CONFIG_FILE = "config.json"

	data = bu.mcXLSchoolDistInput(os.path.join(DATADIR,DATAFILE))
	cfg = cp.mcModelConfig(CONFIG_FILE)

	strategy = mcAlgorithmV2() if cfg.model_variant() == "v2" else None
	
	grouper = CEPSchoolGroupGenerator(cfg,strategy)
	groups = grouper.get_groups(data)

	print(json.dumps(groups, indent=2))

#end: main

if __name__ == "__main__":
	main()
else:
	# do nothing
	pass