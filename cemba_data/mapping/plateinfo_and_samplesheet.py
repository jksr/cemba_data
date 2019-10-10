"""
Contain codes about parse plate info and generate sample sheet
"""

import pathlib
from collections import OrderedDict

import pandas as pd

import cemba_data

PACKAGE_DIR = pathlib.Path(cemba_data.__path__[0])

with open(PACKAGE_DIR / 'mapping/files/sample_sheet_header.txt') as _f:
    SAMPLESHEET_DEFAULT_HEADER = _f.read()

SECTIONS = ['[CriticalInfo]', '[LibraryInfo]', '[PlateInfo]']
LIMITED_CHOICES = {
    'n_random_index': [8, 384, '8', '384'],
    'input_plate_size': [384, '384'],
    'primer_quarter': ['Set1_Q1', 'Set1_Q2', 'Set1_Q3', 'Set1_Q4',
                       'SetB_Q1', 'SetB_Q2', 'SetB_Q3', 'SetB_Q4']}

CRITICAL_INFO_KEYS = ['n_random_index', 'input_plate_size',
                      'pool_id', 'tube_label', 'email']

# key (n_random_index, input_plate_size)
BARCODE_TABLE = {
    ('8', '384'): PACKAGE_DIR / 'mapping/files/V1_i7_i5_index.tsv',
    ('384', '384'): PACKAGE_DIR / 'mapping/files/V2_i7_i5_index.tsv'
}


def get_kv_pair(line):
    try:
        k, v = line.split('=')
        return k, v
    except ValueError:
        raise ValueError(f'Each key=value line must contain a "=" to separate key and value. Got {line}')


def read_plate_info(plateinfo_path):
    cur_section = ''
    cur_section_id = -1

    critical_info = {}
    library_info = OrderedDict()
    plate_header = True
    plate_info = []

    with open(plateinfo_path) as f:
        for line in f:
            line = line.strip('\n')
            if line == '' or line.startswith('#'):
                continue
            # print(line)

            # determine section
            if line.startswith('['):
                cur_section_id += 1
                if line == SECTIONS[cur_section_id]:
                    cur_section = line
                else:
                    raise ValueError(
                        f'Section name and order must be [CriticalInfo] [LibraryInfo] [PlateInfo], '
                        f'got {line} at No.{cur_section_id + 1} section.')
            elif cur_section == '[CriticalInfo]':
                k, v = get_kv_pair(line)
                if k not in CRITICAL_INFO_KEYS:
                    raise ValueError(f'Unknown key {k} in [CriticalInfo]')
                else:
                    critical_info[k] = v
            elif cur_section == '[LibraryInfo]':
                k, v = get_kv_pair(line)
                if (k in critical_info.keys()) or (k in library_info.keys()):
                    raise ValueError(f'Found duplicated key {k}')
                else:
                    library_info[k] = v
            elif cur_section == '[PlateInfo]':
                ll = line.split('\t')
                if plate_header:
                    plate_header = False
                plate_info.append(ll)
            else:
                raise ValueError(f'Got a malformed line: {line}')

    for k in CRITICAL_INFO_KEYS:
        if k not in critical_info:
            raise ValueError(f'[CriticalInfo] missing key-value pair "{k}"')

    header = plate_info[0]
    plate_info = pd.DataFrame(plate_info[1:], columns=plate_info[0])
    for k, v in library_info.items():
        if k in plate_info.columns:
            raise ValueError(f'Found duplicated key {k} between [PlateInfo] and [LibraryInfo]')
        plate_info[k] = v

    if critical_info['n_random_index'] == '8':
        n_plate_info_fix_col = 2
    elif critical_info['n_random_index'] == '384':
        n_plate_info_fix_col = 3
    else:
        raise ValueError(f'[CriticalInfo] n_random_index got unknown value '
                         f'{critical_info["n_random_index"]}')
    col_order = header[:n_plate_info_fix_col] + list(library_info.keys()) + header[n_plate_info_fix_col:]
    plate_info = plate_info[col_order].copy()
    plate_info['sample_id_prefix'] = plate_info.apply(
        lambda i: '-'.join(i[n_plate_info_fix_col:].astype(str).tolist()), axis=1)

    # after getting sample_id_prefix, add critical info into plate_info too
    for k, v in critical_info.items():
        if k in plate_info.columns:
            raise ValueError(f'Found duplicated key {k}')
        plate_info[k] = v

    return critical_info, plate_info


def plate_384_random_index_8(plate_info, barcode_table):
    records = []

    # check plate_info primer compatibility
    for primer_quarter, n_plate in plate_info['primer_quarter'].value_counts().iteritems():
        if n_plate < 2:
            raise ValueError(f'{primer_quarter} only have 1 plate in the table, are you really sure?')
        elif n_plate == 2:
            pass
        else:
            raise ValueError(f'{primer_quarter} have {n_plate} plates in the table, that is impossible.')

    for primer_quarter, plate_pair in plate_info.groupby('primer_quarter'):
        if primer_quarter not in LIMITED_CHOICES['primer_quarter']:
            raise ValueError(f'Unknown primer_quarter value {primer_quarter}')

        plate1, plate2 = plate_pair['plate_id']

        # check plate pair info consistence
        for col_name, col in plate_pair.iteritems():
            if col.unique().size != 1:
                if col_name != 'plate_id':
                    print(f'{col_name} contains different information between {plate1} and {plate2}, '
                          f'Will put {plate1} prefix into sample_id. This should not happen normally.')

        # remove all the '-' with '_' in plate names
        plate1 = plate1.replace('-', '_')
        plate2 = plate2.replace('-', '_')

        for col in 'ABCDEFGH':
            for row in range(1, 13):
                plate_pos = f'{col}{row}'
                cur_row = barcode_table.loc[(primer_quarter, plate_pos)]
                i5_barcode = cur_row['i5_index_sequence']
                i7_barcode = cur_row['i7_index_sequence']
                sample_id_prefix = plate_pair['sample_id_prefix'].iloc[0]
                sample_id = f'{sample_id_prefix}-{plate1}-{plate2}-{plate_pos}'

                # THIS IS BASED ON FORMAT BCL2FASTQ NEEDS
                records.append({'Sample_ID': sample_id,
                                'index': i7_barcode,  # the index must be i7
                                'index2': i5_barcode,  # the index2 must be i5
                                'Sample_Project': plate_pair['tube_label'].iloc[0],
                                'Description': plate_pair['email'].iloc[0]})
    # THIS IS BASED ON FORMAT BCL2FASTQ NEEDS
    sample_sheet = pd.DataFrame(records)
    sample_sheet['Sample_Name'] = ''
    sample_sheet['Sample_Well'] = ''
    sample_sheet['I7_Index_ID'] = ''
    sample_sheet['I5_Index_ID'] = ''
    sample_sheet['I7_Index_ID'] = ''
    sample_sheet['Sample_Plate'] = 'Plate'

    miseq_sample_sheet = sample_sheet[['Sample_ID', 'Sample_Name', 'Sample_Plate',
                                       'Sample_Well', 'I7_Index_ID', 'index',
                                       'I5_Index_ID', 'index2', 'Sample_Project',
                                       'Description']].copy()

    lanes = []
    for i in range(1, 5):
        lane_df = miseq_sample_sheet.copy()
        lane_df['Lane'] = i
        lanes.append(lane_df)
    nova_sample_sheet = pd.concat(lanes)
    nova_sample_sheet = nova_sample_sheet[['Lane', 'Sample_ID', 'Sample_Name', 'Sample_Plate',
                                           'Sample_Well', 'I7_Index_ID', 'index',
                                           'I5_Index_ID', 'index2', 'Sample_Project',
                                           'Description']].copy()

    return miseq_sample_sheet, nova_sample_sheet


def plate_384_random_index_384(plate_info, barcode_table):
    records = []

    # check plate_info primer compatibility
    for primer_name, n_primer in plate_info['primer_name'].value_counts().iteritems():
        if n_primer > 1:
            raise ValueError(f'{primer_name} have {n_primer} multiplex_group in the table, that is impossible.')

    for _, row in plate_info.iterrows():
        plate = row['plate_id']
        # remove all the '-' with '_' in plate names
        plate = plate.replace('-', '_')

        barcode_name = row['primer_name']
        cur_row = barcode_table.loc[barcode_name]
        i5_barcode = cur_row['i5_index_sequence']
        i7_barcode = cur_row['i7_index_sequence']
        sample_id_prefix = row['sample_id_prefix']
        multiplex_group = row['multiplex_group']
        sample_id = f'{sample_id_prefix}-{plate}-{multiplex_group}-{barcode_name}'

        # THIS IS BASED ON FORMAT BCL2FASTQ NEEDS
        records.append({'Sample_ID': sample_id,
                        'index': i7_barcode,  # the index must be i7
                        'index2': i5_barcode,  # the index2 must be i5
                        'Sample_Project': row['tube_label'],
                        'Description': row['email']})
    # THIS IS BASED ON FORMAT BCL2FASTQ NEEDS
    sample_sheet = pd.DataFrame(records)
    sample_sheet['Sample_Name'] = ''
    sample_sheet['Sample_Well'] = ''
    sample_sheet['I7_Index_ID'] = ''
    sample_sheet['I5_Index_ID'] = ''
    sample_sheet['I7_Index_ID'] = ''
    sample_sheet['Sample_Plate'] = 'Plate'

    miseq_sample_sheet = sample_sheet[['Sample_ID', 'Sample_Name', 'Sample_Plate',
                                       'Sample_Well', 'I7_Index_ID', 'index',
                                       'I5_Index_ID', 'index2', 'Sample_Project',
                                       'Description']].copy()

    lanes = []
    for i in range(1, 5):
        lane_df = miseq_sample_sheet.copy()
        lane_df['Lane'] = i
        lanes.append(lane_df)
    nova_sample_sheet = pd.concat(lanes)
    nova_sample_sheet = nova_sample_sheet[['Lane', 'Sample_ID', 'Sample_Name', 'Sample_Plate',
                                           'Sample_Well', 'I7_Index_ID', 'index',
                                           'I5_Index_ID', 'index2', 'Sample_Project',
                                           'Description']].copy()

    return miseq_sample_sheet, nova_sample_sheet


def make_sample_sheet(plate_info_paths, output_prefix, header_path=None):
    if isinstance(plate_info_paths, str):
        plate_info_paths = [plate_info_paths]
    critical_infos = []
    plate_infos = []

    miseq_sample_sheets = []
    nova_sample_sheets = []

    for plate_info_path in plate_info_paths:
        critical_info, plate_info = read_plate_info(plate_info_path)
        critical_infos.append(critical_info)
        plate_infos.append(plate_info)

        # check valid choice
        for k in ['n_random_index', 'input_plate_size']:
            if critical_info[k] not in LIMITED_CHOICES[k]:
                raise ValueError(f'Invalid value in critical_info section for {k}: {critical_info[k]}')

        n_random_index = critical_info['n_random_index']
        input_plate_size = critical_info['input_plate_size']

        barcode_table_path = BARCODE_TABLE[n_random_index, input_plate_size]
        if (critical_info['n_random_index'], critical_info['input_plate_size']) == ('8', '384'):
            barcode_table = pd.read_csv(barcode_table_path, sep='\t')
            barcode_table['primer_quarter'] = barcode_table['Index_set'] + "_" + barcode_table['Index_quarter']
            barcode_table.set_index(['primer_quarter', 'plate_pos'], inplace=True)
            miseq_sample_sheet, nova_sample_sheet = plate_384_random_index_8(plate_info, barcode_table)
        elif (critical_info['n_random_index'], critical_info['input_plate_size']) == ('384', '384'):
            barcode_table = pd.read_csv(barcode_table_path,
                                        sep='\t', index_col='set_384_plate_pos')
            miseq_sample_sheet, nova_sample_sheet = plate_384_random_index_384(plate_info, barcode_table)
        else:
            raise NotImplementedError(f"Unknown combination of n_random_index {critical_info['n_random_index']} "
                                      f"and input_plate_size {critical_info['input_plate_size']}")
        miseq_sample_sheets.append(miseq_sample_sheet)
        nova_sample_sheets.append(nova_sample_sheet)
    miseq_sample_sheet = pd.concat(miseq_sample_sheets)
    nova_sample_sheet = pd.concat(nova_sample_sheets)

    # before write out, check plate info compatibility:
    total_plate_info = pd.concat(plate_infos)
    # check plate_info primer compatibility
    if int(n_random_index) == 8:
        for primer_quarter, n_plate in total_plate_info['primer_quarter'].value_counts().iteritems():
            if n_plate < 2:
                raise ValueError(f'{primer_quarter} only have 1 plate in the table, are you really sure?')
            elif n_plate == 2:
                pass
            else:
                raise ValueError(f'{primer_quarter} have {n_plate} plates in the table, that is impossible.')
    elif int(n_random_index) == 384:
        for primer_name, n_primer in total_plate_info['primer_name'].value_counts().iteritems():
            if n_primer > 1:
                raise ValueError(f'{primer_name} have {n_primer} multiplex_group in the table, that is impossible.')
    else:
        # should be raised above already
        raise

    # write miseq sample sheet
    with open(output_prefix + '.miseq.sample_sheet.csv', 'w') as output_f:
        if header_path is None:
            output_f.write(SAMPLESHEET_DEFAULT_HEADER)
        else:
            with open(header_path) as hf:
                output_f.write(hf.read())
        output_f.write(miseq_sample_sheet.to_csv(index=None))

    # write novaseq sample sheet
    with open(output_prefix + '.novaseq.sample_sheet.csv', 'w') as output_f:
        if header_path is None:
            output_f.write(SAMPLESHEET_DEFAULT_HEADER)
        else:
            with open(header_path) as hf:
                output_f.write(hf.read())
        output_f.write(nova_sample_sheet.to_csv(index=None))
    return


def print_plate_info(primer_version):
    if primer_version.upper() == 'V1':
        with open(PACKAGE_DIR / 'mapping/files/plate_info_template_v1.txt') as _f:
            template = _f.read()
    elif primer_version.upper() == 'V2':
        with open(PACKAGE_DIR / 'mapping/files/plate_info_template_v2.txt') as _f:
            template = _f.read()
    print(template)
    return