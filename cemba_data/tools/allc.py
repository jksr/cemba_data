from .utilities import *
import gzip
from functools import partial
from pybedtools import BedTool, cleanup
from subprocess import run, PIPE
import shlex
from collections import defaultdict
import pandas as pd
import numpy as np
import os
import pathlib
from .open import open_allc
from concurrent.futures import ProcessPoolExecutor, as_completed


def _split_to_chrom_bed(allc_path, context_pattern, genome_size_path,
                        out_path_prefix, max_cov_cutoff=None):
    """
    Split ALLC into bed format, chrom column contain "chr".
    :param allc_path: Single ALLC file path
    :param context_pattern: comma separate context patterns or list
    :param out_path_prefix: Single output prefix
    :param max_cov_cutoff: 2 for single cell, None for bulk or merged allc
    :param genome_size_path: UCSC chrom size file path
    :return: Path dict for out put files
    """
    chrom_set = set(parse_chrom_size(genome_size_path).keys())

    # deal with some old allc file that don't have chr in chrom name
    ref_chrom_have_chr = False
    for chrom in chrom_set:
        if chrom.startswith('chr'):
            ref_chrom_have_chr = True
        # if any chrom in the genome_size_path have chr, treat the whole reference as ref_chrom_have_chr
    # whether add chr or not depending on this, judged later in the first line
    need_to_add_chr = False

    # prepare context
    if isinstance(context_pattern, str):
        context_pattern = context_pattern.split(',')
    pattern_dict = {c: parse_mc_pattern(c) for c in context_pattern}

    # prepare out path
    path_dict = {(c, chrom): out_path_prefix + f'.{c}.{chrom}.bed'
                 for c in context_pattern
                 for chrom in chrom_set}

    # open all paths
    open_func = partial(open, mode='a')
    # open func for allc:
    if '.gz' in allc_path[-3:]:
        allc_open_func = partial(gzip.open, mode='rt')
    else:
        allc_open_func = partial(open, mode='r')

    # split ALLC
    first = True
    cur_chrom = None
    handle_dict = None
    with allc_open_func(allc_path) as allc:
        for line in allc:
            if first:
                chrom = line.split('\t')[0]
                # judge if the first line have chr or not, if not,
                # but ref_chrom_have_chr is true, add chr for every line
                if ref_chrom_have_chr and not chrom.startswith('chr'):
                    need_to_add_chr = True
                    chrom = 'chr' + chrom
                if chrom not in chrom_set:
                    continue
                first = False
                cur_chrom = chrom
                handle_dict = {c: open_func(path_dict[(c, cur_chrom)]) for c in context_pattern}
            ll = line.split('\t')
            # filter max cov (for single cell data)
            if (max_cov_cutoff is not None) and (int(ll[5]) > max_cov_cutoff):
                continue
            # judge chrom
            chrom = ll[0]
            if need_to_add_chr:
                chrom = 'chr' + chrom
            if chrom not in chrom_set:
                continue
            if chrom != cur_chrom:
                cur_chrom = chrom
                for handle in handle_dict.values():
                    handle.close()
                handle_dict = {c: open_func(path_dict[(c, cur_chrom)]) for c in context_pattern}
            # bed format [chrom, start, end, mc, cov]
            ll[1] = str(int(ll[1]) - 1)  # because bed is 0 based
            bed_line = '\t'.join([chrom, ll[1], ll[1], ll[4], ll[5]]) + '\n'
            # assign each line to its patten content,
            # will write multiple times if patten overlap
            for c, p in pattern_dict.items():
                if ll[3] in p:
                    handle_dict[c].write(bed_line)
    # close handle
    for handle in handle_dict.values():
        handle.close()
    return path_dict


# TODO: change map to region using tabix, prevent output temp file and include parallel
def map_to_region(allc_path, out_path_prefix,
                  region_bed_path, region_name, genome_size_path,
                  context_pattern, max_cov_cutoff, remove_tmp):
    """
    Map one allc file into many region set bed file using bedtools map.
    Count mC and coverage in each region for each context pattern.

    Parameters
    ----------
    allc_path
    out_path_prefix
    region_bed_path
    region_name
    genome_size_path
        UCSC chrom.sizes file, will determine which chrom to keep in the output.
        Use main chrom if want to remove those random contigs
    context_pattern
    max_cov_cutoff
    remove_tmp

    Returns
    -------

    """

    # parse ref chrom with ordered chromosome
    ref_chrom_dict = parse_chrom_size(genome_size_path)

    # prepare ALLC bed dict, split ALLC into different contexts
    # bed format [chrom, start, end, mc, cov]
    print('Splitting ALLC')
    # split chromosome and avoid sorting
    allc_bed_path_dict = _split_to_chrom_bed(allc_path=allc_path,
                                             context_pattern=context_pattern,
                                             out_path_prefix=out_path_prefix + '.tmp',
                                             genome_size_path=genome_size_path,
                                             max_cov_cutoff=max_cov_cutoff)
    # concat bed with ordered chromosome
    tmp_dict = {}
    for c in context_pattern:
        c_path_list = [allc_bed_path_dict[(c, _chrom)]
                       for _chrom in ref_chrom_dict.keys()
                       if (c, _chrom) in allc_bed_path_dict]
        cmd = ['cat'] + c_path_list
        concat_bed_path = out_path_prefix + f'.{c}.tmp.total.bed'
        with open(concat_bed_path, 'w') as fh:
            run(cmd, stdout=fh)
            for p in c_path_list:
                run(['rm', '-f', p])
        tmp_dict[c] = concat_bed_path
    allc_bed_path_dict = tmp_dict

    print('Reading ALLC Bed')
    allc_bed_dict = {k: BedTool(path) for k, path in allc_bed_path_dict.items()}
    # k is (context_pattern)

    # prepare all region bed files
    print('Reading Region Bed')
    if len(region_bed_path) != len(region_name):
        raise ValueError('Number of region BED path != Number of region names')
    # input region bed, sort across allc chrom order
    # chrom_order is in UCSC genome size format,
    # make a bed format from it and then intersect with bed_p to filter out chromosomes not appear in ALLC

    region_bed_dict = {region_n: BedTool(bed_p).sort(g=genome_size_path)
                       for bed_p, region_n in zip(region_bed_path, region_name)}

    # bedtools map
    for context_name, allc_bed in allc_bed_dict.items():
        for region_name, region_bed in region_bed_dict.items():
            print(f'Map {context_name} ALLC Bed to {region_name} Region Bed')
            region_bed.map(b=allc_bed, c='4,5', o='sum,sum', g=genome_size_path) \
                .saveas(out_path_prefix + f'.{region_name}_{context_name}.count_table.bed.gz',
                        compressed=True)

    # cleanup the tmp bed files.
    if remove_tmp:
        print('Clean tmp Bed file')
        for path in allc_bed_path_dict.values():
            run(['rm', '-f', path])
    cleanup()  # pybedtools tmp files
    print('Finish')
    return


def allc_to_bigwig(allc_path, out_path, chrom_size, mc_type='CGN'):
    # TODO add allc to bigwig COV version, not calculate mC but only compute cov
    from .allc_utilities import convert_allc_to_bigwig
    convert_allc_to_bigwig(allc_path,
                           out_path,
                           chrom_size,
                           mc_type=mc_type,
                           bin_size=100,
                           path_to_wigtobigwig="",
                           min_bin_sites=0,
                           min_bin_cov=0,
                           max_site_cov=None,
                           min_site_cov=0,
                           add_chr_prefix=True)
    return


def extract_context_allc(allc_path, out_path, merge_strand=True, mc_context='CGN'):
    # TODO support multiple context
    if isinstance(mc_context, list):
        if len(mc_context) > 1:
            raise NotImplementedError('TODO support multiple context')
        mc_context = mc_context[0]

    if 'CG' not in mc_context:
        merge_strand = False

    if allc_path.endswith('gz'):
        opener = partial(gzip.open, mode='rt')
    else:
        opener = partial(open, mode='r')
    writer = partial(gzip.open, mode='wt')

    context_set = parse_mc_pattern(mc_context)
    with opener(allc_path) as allc, \
            writer(out_path) as out_allc:
        if merge_strand:
            prev_line = None
            cur_chrom = None
            for line in allc:
                cur_line = line.strip('\n').split('\t')
                if cur_line[3] not in context_set:
                    continue
                if cur_line[0] != cur_chrom:
                    if prev_line is not None:
                        out_allc.write('\t'.join(prev_line) + '\n')
                    prev_line = cur_line
                    cur_chrom = cur_line[0]
                    continue
                if prev_line is None:
                    prev_line = cur_line
                    continue
                else:
                    # pos should be continuous, strand should be reverse
                    if int(prev_line[1]) + 1 == int(cur_line[1]) and prev_line[2] != cur_line[2]:
                        new_line = prev_line[:4] + [str(int(prev_line[4]) + int(cur_line[4])),
                                                    str(int(prev_line[5]) + int(cur_line[5])), '1']
                        out_allc.write('\t'.join(new_line) + '\n')
                        prev_line = None
                    # otherwise, only write and update prev_line
                    else:
                        out_allc.write('\t'.join(prev_line) + '\n')
                        prev_line = cur_line
        else:
            for line in allc:
                cur_line = line.strip('\n').split('\t')
                if cur_line[3] not in context_set:
                    continue
                out_allc.write('\t'.join(cur_line) + '\n')
    print(f'Extract {mc_context} finished:', out_path)
    return


def get_allc_profile(allc_path, drop_n=True, n_rows=100000000, out_path=None):
    """
    Generate approximate profile for allc file. 1e8 rows finish in about 5 min.

    Parameters
    ----------
    allc_path
        path of the allc file
    drop_n
        whether drop context contain N
    n_rows
        number of rows to use, 1e8 is sufficient to get an approximate profile
    out_path
        if not None, save profile to out_path
    Returns
    -------

    """
    if 'gz' in allc_path:
        opener = partial(gzip.open, mode='rt')
    else:
        opener = partial(open, mode='r')

    # initialize count dict
    mc_sum_dict = defaultdict(int)
    cov_sum_dict = defaultdict(int)
    cov_sum2_dict = defaultdict(int)  # sum of square, for calculating variance
    rate_sum_dict = defaultdict(float)
    rate_sum2_dict = defaultdict(float)  # sum of square, for calculating variance
    context_count_dict = defaultdict(int)
    with opener(allc_path) as f:
        n = 0
        for line in f:
            chrom, pos, strand, context, mc, cov, p = line.split('\t')
            if drop_n and 'N' in context:
                continue
            # mc and cov
            mc_sum_dict[context] += int(mc)
            cov_sum_dict[context] += int(cov)
            cov_sum2_dict[context] += int(cov) ** 2
            # raw base rate
            rate = int(mc) / int(cov)
            rate_sum_dict[context] += rate
            rate_sum2_dict[context] += rate ** 2
            # count context finally
            context_count_dict[context] += 1
            n += 1
            if (n_rows is not None) and (n >= n_rows):
                break
    # overall count
    profile_df = pd.DataFrame({'partial_mc': mc_sum_dict,
                               'partial_cov': cov_sum_dict})
    profile_df['base_count'] = pd.Series(context_count_dict)
    profile_df['overall_mc_rate'] = profile_df['partial_mc'] / profile_df['partial_cov']

    # cov base mean and base std.
    # assume that base cov follows normal distribution
    cov_sum_series = pd.Series(cov_sum_dict)
    cov_sum2_series = pd.Series(cov_sum2_dict)
    profile_df['base_cov_mean'] = cov_sum_series / profile_df['base_count']
    profile_df['base_cov_std'] = np.sqrt(
        (cov_sum2_series / profile_df['base_count']) - profile_df['base_cov_mean'] ** 2)

    # assume that base rate follow beta distribution
    # so that observed rate actually follow joint distribution of beta (rate) and normal (cov) distribution
    # here we use the observed base_rate_mean and base_rate_var to calculate
    # approximate alpha and beta value for the base rate beta distribution
    rate_sum_series = pd.Series(rate_sum_dict)
    rate_sum2_series = pd.Series(rate_sum2_dict)
    profile_df['base_rate_mean'] = rate_sum_series / profile_df['base_count']
    profile_df['base_rate_var'] = (rate_sum2_series / profile_df['base_count']) - profile_df['base_rate_mean'] ** 2

    # based on beta distribution mean, var
    # a / (a + b) = base_rate_mean
    # a * b / ((a + b) ^ 2 * (a + b + 1)) = base_rate_var
    # we have:
    a = (1 - profile_df['base_rate_mean']) * (profile_df['base_rate_mean'] ** 2) / profile_df['base_rate_var'] - \
        profile_df['base_rate_mean']
    b = a * (1 / profile_df['base_rate_mean'] - 1)
    profile_df['base_beta_a'] = a
    profile_df['base_beta_b'] = b

    if out_path is not None:
        profile_df.to_csv(out_path, sep='\t')
        return None
    else:
        return profile_df


def tabix_allc(allc_path, reindex=False):
    if os.path.exists(f'{allc_path}.tbi') and not reindex:
        return
    run(shlex.split(f'tabix -b 2 -e 2 -s 1 {allc_path}'),
        check=True)
    return


def get_md5(file_path):
    file_md5 = run(shlex.split(f'md5sum {file_path}'), stdout=PIPE, encoding='utf8', check=True).stdout
    file_md5 = file_md5.split(' ')[0]
    return file_md5


def standardize_allc(allc_path, genome_size_path, compress_level=6,
                     idx=True, remove_additional_chrom=False):
    # if tabix exist and newer than allc, skip and return md5
    if os.path.exists(allc_path + '.tbi'):
        tbi_time = os.path.getmtime(allc_path + '.tbi')
        allc_time = os.path.getmtime(allc_path)
        if allc_time < tbi_time:
            file_md5 = get_md5(allc_path)
            return file_md5

    genome_dict = parse_chrom_size(genome_size_path)
    if 'chr1' in genome_dict:
        raw_add_chr = True
    else:
        raw_add_chr = False
    with open_allc(allc_path) as f, \
            open_allc(allc_path + '.tmp.gz', mode='w',
                      compresslevel=compress_level) as wf:
        cur_chrom = "TOTALLY_NOT_A_CHROM"
        cur_start = cur_chrom + '\t'
        cur_pointer = 0
        index_lines = []
        buffer_lines = ''
        line_count = 0
        add_chr = raw_add_chr
        for line in f:
            if line_count == 0:
                # for very old allc files, which contain header line
                ll = line.split('\t')
                try:
                    int(ll[1])  # pos
                    int(ll[4])  # mc
                    int(ll[5])  # cov
                except ValueError:
                    # The first line is header, remove header
                    continue
            if line_count < 2:
                # 1st line could be header that startswith chr
                # so check 1st and 2nd row
                if line.startswith('chr'):
                    add_chr = False
            if add_chr:
                line = 'chr' + line
            if not line.startswith(cur_start):
                fields = line.split("\t")
                cur_chrom = fields[0]
                if (cur_chrom not in genome_dict) and (not remove_additional_chrom):
                    raise KeyError(f'{cur_chrom} not exist in genome size file, '
                                   f'set remove_additional_chrom=True if want to remove additional chroms')
                index_lines.append(cur_chrom + "\t" + str(cur_pointer) + "\n")
                cur_start = cur_chrom + '\t'
            cur_pointer += len(line)
            buffer_lines += line
            line_count += 1
            if line_count % 50000 == 0:
                wf.write(buffer_lines)
                buffer_lines = ''
        wf.write(buffer_lines)

    run(shlex.split(f'mv {allc_path} {allc_path}.bp'), check=True)
    run(shlex.split(f'mv {allc_path}.tmp.gz {allc_path}'), check=True)
    run(shlex.split(f'rm -f {allc_path}.bp'), check=True)
    if idx:
        # backward compatibility
        index_lines.append("#eof\n")
        with open(allc_path + '.idx', 'w') as idxf:
            idxf.writelines(index_lines)
    else:
        if os.path.exists(allc_path + '.idx'):
            run(shlex.split(f'rm -f {allc_path}.idx'), check=True)
    tabix_allc(allc_path, reindex=True)

    file_md5 = get_md5(allc_path)
    return file_md5


def batch_standardize_allc(allc_dir, genome_size_path, compress_level=6,
                           idx=True, remove_additional_chrom=False, process=10):
    allc_paths = list(pathlib.Path(allc_dir).glob('**/allc*tsv.gz'))
    with ProcessPoolExecutor(max_workers=process) as executor:
        future_result = {executor.submit(standardize_allc,
                                         allc_path=str(allc_path),
                                         genome_size_path=genome_size_path,
                                         idx=idx, remove_additional_chrom=remove_additional_chrom,
                                         compress_level=compress_level): allc_path
                         for allc_path in allc_paths}
    md5_dict = {}
    for future in as_completed(future_result):
        allc_path = future_result[future]
        try:
            data = future.result()
        except OSError as exc:
            print(f'{allc_path} generated an exception: {exc}')
        else:
            md5_dict[str(allc_path)] = data

    with open(allc_dir + '/md5_list.txt', 'w') as f:
        for k, v in md5_dict.items():
            f.write('\t'.join([k, v]) + '\n')
    return md5_dict
