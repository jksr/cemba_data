import logging
import operator
import pathlib

import pandas as pd

import cemba_data
from .fastq_qc import summarize_fastq_qc
from .utilities import get_configuration

# logger
log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

PACKAGE_DIR = pathlib.Path(cemba_data.__path__[0])


def star_mapping(input_dir, output_dir, config):
    """
    reads level QC and trimming. R1 R2 separately.
    """
    output_dir = pathlib.Path(output_dir)
    input_dir = pathlib.Path(input_dir)
    fastq_qc_records = pd.read_csv(input_dir / 'fastq_qc.records.csv',
                                   index_col=['uid', 'index_name', 'read_type'], squeeze=True)
    fastq_qc_stats_path = summarize_fastq_qc(input_dir)
    fastq_qc_stats = pd.read_csv(fastq_qc_stats_path, index_col=0)

    if isinstance(config, str):
        config = get_configuration(config)

    star_reference = config['star']['star_reference']
    read_min = int(config['star']['read_min'])
    read_max = int(config['star']['read_max'])
    threads = int(config['star']['threads'])

    # sort by total reads, map large sample first
    sample_dict = {}
    for (uid, index_name), sub_df in fastq_qc_stats.sort_values('out_reads').groupby(['uid', 'index_name']):
        sample_dict[(uid, index_name)] = sub_df['out_reads'].astype(int).sum()
    sorted_sample = sorted(sample_dict.items(), key=operator.itemgetter(1), reverse=True)

    records = []
    command_list = []
    for (uid, index_name), total_reads in sorted_sample:
        if index_name == 'unknown':
            continue
        if total_reads < read_min:
            log.info(f"Drop cell due to too less reads: {uid} {index_name}, total reads {total_reads}")
            continue
        if total_reads > read_max:
            log.info(f"Drop cell due to too many reads: {uid} {index_name}, total reads {total_reads}")
            continue

        # for RNA part, only map R1
        r1_fastq = fastq_qc_records[(uid, index_name, 'R1')]
        output_prefix = output_dir / (pathlib.Path(r1_fastq).name[:-6] + '.STAR')
        star_cmd = f'STAR --runThreadN {threads} ' \
                   f'--genomeDir {star_reference} ' \
                   f'--genomeLoad LoadAndKeep ' \
                   f'--alignEndsType EndToEnd ' \
                   f'--outSAMstrandField intronMotif ' \
                   f'--outSAMtype BAM Unsorted ' \
                   f'--outSAMunmapped Within ' \
                   f'--outSAMattributes NH HI AS NM MD ' \
                   f'--sjdbOverhang 100 ' \
                   f'--outFilterType BySJout ' \
                   f'--outFilterMultimapNmax 20 ' \
                   f'--alignSJoverhangMin 8 ' \
                   f'--alignSJDBoverhangMin 1 ' \
                   f'--outFilterMismatchNmax 999 ' \
                   f'--outFilterMismatchNoverLmax 0.04 ' \
                   f'--alignIntronMin 20 ' \
                   f'--alignIntronMax 1000000 ' \
                   f'--alignMatesGapMax 1000000 ' \
                   f'--outFileNamePrefix {output_prefix} ' \
                   f'--readFilesIn {r1_fastq} ' \
                   f'--readFilesCommand gzip -cd'
        records.append([uid, index_name, output_prefix])
        command_list.append(star_cmd)

    with open(output_dir / 'star_mapping.command.txt', 'w') as f:
        f.write('\n'.join(command_list))
    record_df = pd.DataFrame(records,
                             columns=['uid', 'index_name', 'bam_path'])
    record_df.to_csv(output_dir / 'star_mapping.records.csv', index=None)
    return
