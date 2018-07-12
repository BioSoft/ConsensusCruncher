#!/usr/bin/env python3

# ===================================================================================
#
#  FILE:         ConsensusCruncher.py
#
#  USAGE:
#  ConsensusCruncher.py fastq2bam -i input_dir -o output_dir
#
#
#  ConsensusCruncher.py consensus -i input_dir -o output_dir
#
#
#  OPTIONS:
#
#    -i  Input bamfile directory [MANDATORY]
#    -o  Output project directory [MANDATORY]
#    -s  Singleton correction, default: ON (use "OFF" to disable)
#    -b  Bedfile, default: cytoBand.txt
#        WARNING: It is HIGHLY RECOMMENDED that you use the default cytoBand.txt and
#        not to include your own bedfile. This option is mainly intended for non-human
#        genomes, where a separate bedfile is needed for data segmentation. If you do
#        choose to use your own bedfile, please format with the bed_separator.R tool.
#
#        For small or non-human genomes where cytobands cannot be used for segmenting the
#        data set, you may choose to turn off this option with "-b OFF" and process the
#        data all at once (Division of data is only required for large data sets to offload
#        the memory burden).
#
#    -c  Consensus cut-off, default: 0.7 (70% of reads must have the same base to form
#        a consensus)
#    -q  qusb directory, default: output/qsub
#    -h  Show this message
#
#  DESCRIPTION:
#
#  This script amalgamates duplicate reads in bamfiles into single-strand consensus
#  sequences (SSCS), which are subsequently combined into duplex consensus sequences
#  (DCS). Singletons (reads lacking duplicate sequences) are corrected, combined
#  with SSCS to form SSCS + SC, and further collapsed to form DCS + SC. Finally,
#  files containing all unique molecules (a.k.a. no duplicates) are created for SSCS
#  and DCS.
#
#  Note: Script will create a "consensus" directory under the project directory and
#  sub-directories corresponding to each bamfile in the input directory.
#
# ===================================================================================

import os
import sys
import re
import argparse
import configparser
from subprocess import Popen, PIPE, call


def fastq2bam(args):
    """
    Extract molecular barcodes from paired-end sequencing reads using a barcode list,
    pattern, or the two combined. Remove constant spacer bases and combine paired
    barcodes before adding to the header of each read in FASTQ files.

    Barcode-extracted FASTQ files are written to the 'fastq_tag' directory and are
    subsequenntly aligned with BWA. BAM files are written to a 'bamfile' directory
    under the specified project folder.

    BARCODE DESIGN:
    You can input either a barcode list or barcode pattern or both. If both are provided, barcodes will first be matched
    with the list and then the constant spacer bases will be removed before the barcode is added to the header.

    N = random / barcode base
    A | C | G | T = constant spacer bases
    e.g. ATNNGT means barcode is flanked by two spacers matching 'AT' in front and 'GT' behind.
    """
    # Create directory for barcode extracted FASTQ files and BAM files
    fastq_dir = '{}/fastq_tag'.format(args.output)
    bam_dir = '{}/bamfiles'.format(args.output)

    # Check if dir exists and there's permission to write
    if not os.path.exists(fastq_dir) and os.access(args.output, os.W_OK):
        os.makedirs(fastq_dir)
    if not os.path.exists(bam_dir) and os.access(args.output, os.W_OK):
        os.makedirs(bam_dir)

    # Set file variables
    filename = os.path.basename(args.fastq1).split(args.name, 1)[0]
    outfile = "{}/{}".format(fastq_dir, filename)

    ####################
    # Extract barcodes #
    ####################
    if args.blist is not None and args.bpattern is not None:
        os.system("{}/ConsensusCruncher/extract_barcodes.py --read1 {} --read2 {} --outfile {} --bpattern {} "
                  "--blist {}".format(code_dir, args.fastq1, args.fastq2, outfile, args.bpattern, args.blist))
    elif args.blist is None:
        os.system("{}/ConsensusCruncher/extract_barcodes.py --read1 {} --read2 {} --outfile {} --bpattern {}".format(
            code_dir, args.fastq1, args.fastq2, outfile, args.bpattern))
    else:
        os.system("{}/ConsensusCruncher/extract_barcodes.py --read1 {} --read2 {} --outfile {} --blist {}".format(
            code_dir, args.fastq1, args.fastq2, outfile, args.blist))

    #############
    # BWA Align #
    #############
    # Command split into chunks and bwa_id retained as str repr
    bwa_cmd = args.bwa + ' mem -M -t4 -R'
    bwa_id = "@RG\tID:1\tSM:" + filename + "\tPL:Illumina"
    bwa_args = '{} {}_barcode_R1.fastq {}_barcode_R2.fastq'.format(args.ref, outfile, outfile)

    bwa = Popen(bwa_cmd.split(' ') + [bwa_id] + bwa_args.split(' '), stdout=PIPE)
    # Sort BAM
    sam1 = Popen((args.samtools + ' view -bhS -').split(' '), stdin=bwa.stdout, stdout=PIPE)
    sam2 = Popen((args.samtools + ' sort -').split(' '), stdin=sam1.stdout,
                 stdout=open('{}/{}.bam'.format(bam_dir, filename), 'w'))
    sam2.communicate()

    # Index BAM
    call("{} index {}/{}.bam".format(args.samtools, bam_dir, filename).split(' '))


def sort_index(bam, samtools):
    """
    Sort and index BAM file.

    :param bam: Path to BAM file.
    :type bam: str
    :param samtools: Path to samtools.
    :type samtools: str
    :returns: Path to sorted BAM file.
    """
    identifier = bam.split('.bam', 1)[0]
    sorted_bam = '{}.sorted.bam'.format(identifier)

    sam1 = Popen((samtools + ' view -bu ' + bam).split(' '), stdout=PIPE)
    sam2 = Popen((samtools + ' sort -').split(' '), stdin=sam1.stdout, stdout=open(sorted_bam, 'w'))
    sam2.communicate()
    os.remove(bam)
    call("{} index {}".format(samtools, sorted_bam).split(' '))

    return sorted_bam


def consensus(args):
    """
    Using unique molecular identifiers (UMIs), duplicate reads from the same molecule are amalgamated into single-strand
    consensus sequences (SSCS). If complementary strands are present, SSCSs can be subsequently combined to form duplex
    consensus sequences (DCS).

    If 'Singleton Correction' (SC) is enabled, single reads (singletons) can be error suppressed using complementary
    strand. These corrected singletons can be merged with SSCSs to be further collapsed into DCSs + SC.

    Finally, a BAM file containing only unique molecules (i.e. no duplicates) is created by merging DCSs, remaining
    SSCSs (those that could not form DCSs), and remaining singletons (those that could not be corrected).
    """
    # Create sample directory to hold consensus sequences
    identifier = os.path.basename(args.bam).split('.bam', 1)[0]
    sample_dir = '{}/{}'.format(args.c_output, identifier)

    # Check if dir exists and there's permission to write
    if not os.path.exists(sample_dir) and os.access(args.output, os.W_OK):
        os.makedirs(sample_dir)

    ########
    # SSCS #
    ########
    # Set variables
    os.makedirs(sample_dir + '/sscs')
    sscs = '{}/sscs/{}.sscs.bam'.format(sample_dir, identifier)
    sing = '{}/sscs/{}.singleton.bam'.format(sample_dir, identifier)

    # Run SSCS_maker
    if args.bedfile is False:
        os.system("{}/ConsensusCruncher/SSCS_maker.py --infile {} --outfile {} --cutoff {}".format(
            code_dir, args.bam, sscs, args.cutoff))
    else:
        os.system("{}/ConsensusCruncher/SSCS_maker.py --infile {} --outfile {} --cutoff {} --bedfile {}".
            format(code_dir, args.bam, sscs, args.cutoff, args.bedfile))

    # Sort and index BAM files
    sscs = sort_index(sscs, args.samtools)
    sing = sort_index(sing, args.samtools)

    #######
    # DCS #
    #######
    # Set variables
    os.makedirs(sample_dir + '/dcs')
    dcs = '{}/dcs/{}.dcs.bam'.format(sample_dir, identifier)
    sscs_sing = '{}/dcs/{}.sscs.singleton.bam'.format(sample_dir, identifier)

    # Move stats and time tracker file to next dir
    os.rename('{}/sscs/{}.stats.txt'.format(sample_dir, identifier),
              '{}/dcs/{}.stats.txt'.format(sample_dir, identifier))
    os.rename('{}/sscs/{}.time_tracker.txt'.format(sample_dir, identifier),
              '{}/dcs/{}.time_tracker.txt'.format(sample_dir, identifier))

    # Run DCS_maker
    if args.bedfile is False:
        os.system("{}/ConsensusCruncher/DCS_maker.py --infile {} --outfile {}".format(code_dir, sscs, dcs))
    else:
        os.system("{}/ConsensusCruncher/DCS_maker.py --infile {} --outfile {} --bedfile {}".format(code_dir, sscs,
                                                                                                   dcs, args.bedfile))

    # Sort and index BAM files
    dcs = sort_index(dcs, args.samtools)
    sscs_sing = sort_index(sscs_sing, args.samtools)

    #############################
    # Singleton Correction (SC) #
    #############################
    if args.scorrect is True:
        os.makedirs(sample_dir + '/sscs_sc')
        # Move stats and time tracker file to next dir
        os.rename('{}/dcs/{}.stats.txt'.format(sample_dir, identifier),
                  '{}/sscs/{}.stats.txt'.format(sample_dir, identifier))
        os.rename('{}/dcs/{}.time_tracker.txt'.format(sample_dir, identifier),
                  '{}/sscs/{}.time_tracker.txt'.format(sample_dir, identifier))

        if args.bedfile is False:
            os.system("{}/ConsensusCruncher/singleton_correction.py --singleton {}".format(code_dir, sing))
        else:
            os.system("{}/ConsensusCruncher/singleton_correction.py --singleton {} --bedfile {}".format(code_dir, sing,
                                                                                                        args.bedfile))
        # Sort and index BAM files
        sscs_cor = '{}/sscs_sc/{}.sscs.correction.bam'.format(sample_dir, identifier)
        os.rename('{}/sscs/{}.sscs.correction.bam'.format(sample_dir, identifier), sscs_cor)
        sscs_cor = sort_index(sscs_cor, args.samtools)

        sing_cor = '{}/sscs_sc/{}.singleton.correction.bam'.format(sample_dir, identifier)
        os.rename('{}/sscs/{}.singleton.correction.bam'.format(sample_dir, identifier), sing_cor)
        sing_cor = sort_index(sing_cor, args.samtools)

        uncorrected = '{}/sscs_sc/{}.uncorrected.bam'.format(sample_dir, identifier)
        os.rename('{}/sscs/{}.uncorrected.bam'.format(sample_dir, identifier), uncorrected)
        uncorrected = sort_index(uncorrected, args.samtools)

        #############
        # SSCS + SC #
        #############
        # Merge corrected singletons with consensus sequences
        sscs_sc = '{}/sscs_sc/{}.sscs.sc.bam'.format(sample_dir, identifier)
        call("{} merge {} {} {} {}".format(args.samtools, sscs_sc, sscs, sscs_cor, sing_cor).split(' '))
        sscs_sc = sort_index(sscs_sc, args.samtools)

        ############
        # DCS + SC #
        ############
        os.makedirs(sample_dir + '/dcs_sc')
        dcs_sc = '{}/dcs_sc/{}.dcs.sc.bam'.format(sample_dir, identifier)
        # Move stats and time tracker file to next dir
        os.rename('{}/sscs/{}.stats.txt'.format(sample_dir, identifier),
                  '{}/dcs_sc/{}.stats.txt'.format(sample_dir, identifier))
        os.rename('{}/sscs/{}.time_tracker.txt'.format(sample_dir, identifier),
                  '{}/dcs_sc/{}.time_tracker.txt'.format(sample_dir, identifier))

        if args.bedfile is False:
            os.system("{}/ConsensusCruncher/DCS_maker.py --infile {} --outfile {}".format(code_dir, sscs_sc, dcs_sc))
        else:
            os.system("{}/ConsensusCruncher/DCS_maker.py --infile {} --outfile {} --bedfile {}".format(
                code_dir, sscs_sc, dcs_sc, args.bedfile))

        # Sort and index BAM files
        dcs_sc = sort_index(dcs_sc, args.samtools)
        sscs_sc_sing = '{}/dcs_sc/{}.sscs.sc.singleton.bam'.format(sample_dir, identifier)
        sscs_sc_sing = sort_index(sscs_sc_sing, args.samtools)

        ########################
        # All Unique Molecules #
        ########################
        # Merge DCS_SC + SSCS_SC singletons + uncorrected singletons
        all_unique = '{}/dcs_sc/{}.all.unique.dcs.bam'.format(sample_dir, identifier)
        call("{} merge {} {} {} {}".format(args.samtools, all_unique, dcs_sc, sscs_sc_sing, uncorrected).split(' '))
        all_unique = sort_index(all_unique, args.samtools)

    # Move stats and time tracker file to sample dir
    os.rename('{}/dcs_sc/{}.stats.txt'.format(sample_dir, identifier),
              '{}/{}.stats.txt'.format(sample_dir, identifier))
    os.rename('{}/dcs_sc/{}.time_tracker.txt'.format(sample_dir, identifier),
              '{}/{}.time_tracker.txt'.format(sample_dir, identifier))
    os.rename('{}/sscs/{}_tag_fam_size.png'.format(sample_dir, identifier),
              '{}/{}_tag_fam_size.png'.format(sample_dir, identifier))
    os.rename('{}/sscs/{}.read_families.txt'.format(sample_dir, identifier),
              '{}/{}.read_families.txt'.format(sample_dir, identifier))

    # Remove intermediate files
    if args.cleanup is True:
        os.remove('{}/{}.time_tracker.txt'.format(sample_dir, identifier))
        os.remove('{}/sssc/{}.badReads.bam'.format(sample_dir, identifier))
        # Remove SSCSs that could not be formed into DCSs
        os.remove('{}/dcs/{}.sscs.singleton.sorted.bam'.format(sample_dir, identifier))
        os.remove('{}/dcs/{}.sscs.singleton.sorted.bam.bai'.format(sample_dir, identifier))
        # Remove singleton correction files and only keep merged files
        os.remove('{}/sssc_sc/{}.singleton.correction.sorted.bam'.format(sample_dir, identifier))
        os.remove('{}/sssc_sc/{}.singleton.correction.sorted.bam.bai'.format(sample_dir, identifier))
        os.remove('{}/sssc_sc/{}.sscs.correction.sorted.bam'.format(sample_dir, identifier))
        os.remove('{}/sssc_sc/{}.sscs.correction.sorted.bam.bai'.format(sample_dir, identifier))
        os.remove('{}/sssc_sc/{}.uncorrected.sorted.bam'.format(sample_dir, identifier))
        os.remove('{}/sssc_sc/{}.uncorrected.sorted.bam.bai'.format(sample_dir, identifier))
        # Remove SSCS_SC that could not be formed into DCSs
        os.remove('{}/dcs_sc/{}.sscs.sc.singleton.sorted.bam'.format(sample_dir, identifier))
        os.remove('{}/dcs_sc/{}.sscs.sc.singleton.sorted.bam.bai'.format(sample_dir, identifier))


if __name__ == '__main__':
    # Mode parser
    main_p = argparse.ArgumentParser()
    main_p.add_argument('-c', '--config', default=None,
                       help="Specify config file. Commandline option overrides config file (Use config template).")
    sub = main_p.add_subparsers(help='sub-command help', dest='subparser_name')

    # Mode help messages
    mode_fastq2bam_help = "Extract molecular barcodes from paired-end sequencing reads using a barcode list and/or " \
                          "a barcode pattern."
    mode_consensus_help = "Almalgamate duplicate reads in BAM files into single-strand consensus sequences (SSCS) and" \
                          " duplex consensus sequences (DCS). Single reads with complementary duplex strands can also" \
                          " be corrected with 'Singleton Correction'."

    # Add subparsers
    sub_a = sub.add_parser('fastq2bam', help=mode_fastq2bam_help, add_help=False)
    sub_b = sub.add_parser('consensus', help=mode_consensus_help, add_help=False)

    # fastq2bam arg help messages
    fastq1_help = "FASTQ containing Read 1 of paired-end reads."
    fastq2_help = "FASTQ containing Read 2 of paired-end reads."
    output_help = "Output directory, where barcode extracted FASTQ and BAM files will be placed in " \
                  "subdirectories 'fastq_tag' and 'bamfiles' respectively (dir will be created if they " \
                  "do not exist)."
    filename_help = "Output filename. If none provided, default will extract output name by taking everything left of" \
                    " '_R'."
    bwa_help = "Path to executable bwa."
    samtools_help = "Path to executable samtools"
    ref_help = "Reference (BWA index)."
    bpattern_help = "Barcode pattern (N = random barcode bases, A|C|G|T = fixed spacer bases)."
    blist_help = "List of barcodes (Text file with unique barcodes on each line)."

    # Consensus arg help messages
    cinput_help = "Input BAM file."
    coutput_help = "Output directory, where a folder will be created for the BAM file and consensus sequences are " \
                   "outputted."
    bedfile_help = "Bedfile, default: cytoBand.txt. WARNING: It is HIGHLY RECOMMENDED that you use the default " \
                   "cytoBand.txt unless you're working with genome build that is not hg19. Then a separate bedfile is" \
                   " needed for data segmentation (file can be formatted with the bed_separator.R tool). For small " \
                   "BAM files, you may choose to turn off data splitting with '-b False' and process everything all at"\
                   " once (Division of data is only required for large data sets to offload the memory burden)."
    cleanup_help = "Remove intermediate files."

    # Determine code directory and set bedfile to split data
    code_dir = os.path.dirname(os.path.realpath(__file__))
    bedfile = '{}/ConsensusCruncher/cytoBand.txt'.format(code_dir)

    # Set args for 'fastq2bam' mode
    sub_args, remaining_args = main_p.parse_known_args()

    if sub_args.config is not None:
        defaults = {"fastq1": fastq1_help,
                    "fastq2": fastq2_help,
                    "output": output_help,
                    "name": "_R",
                    "bwa": bwa_help,
                    "ref": ref_help,
                    "samtools": samtools_help,
                    "bpattern": None,
                    "blist": None,
                    "bam": cinput_help,
                    "c_output": coutput_help,
                    "scorrect": True,
                    "bedfile": bedfile,
                    "cutoff": 0.7,
                    "cleanup": cleanup_help}

        config = configparser.ConfigParser()
        config.read(sub_args.config)

        # Add config file args to fastq2bam mode
        defaults.update(dict(config.items("fastq2bam")))
        sub_a.set_defaults(**defaults)

        # Add config file args to consensus mode
        defaults.update(dict(config.items("consensus")))
        sub_b.set_defaults(**defaults)

    # Parse commandline arguments
    sub_a.add_argument('--fastq1', dest='fastq1', metavar="FASTQ1", type=str, help=fastq1_help)
    sub_a.add_argument('--fastq2', dest='fastq2', metavar="FASTQ2", type=str, help=fastq2_help)
    sub_a.add_argument('-o', '--output', dest='output', metavar="OUTPUT_DIR", type=str, help=output_help)
    sub_a.add_argument('-n', '--name', metavar="FILENAME", type=str, help=filename_help)
    sub_a.add_argument('-b', '--bwa', metavar="BWA", help=bwa_help, type=str)
    sub_a.add_argument('-r', '--ref', metavar="REF", help=ref_help, type=str)
    sub_a.add_argument('-s', '--samtools', metavar="SAMTOOLS", help=samtools_help, type=str)
    sub_a.add_argument('-p', '--bpattern', metavar="BARCODE_PATTERN", type=str, help=bpattern_help)
    sub_a.add_argument('-l', '--blist', metavar="BARCODE_LIST", type=str, help=blist_help)
    sub_a.set_defaults(func=fastq2bam)

    # Set args for 'consensus' mode
    sub_b.add_argument('-i', '--input', metavar="BAM", dest='bam', help=cinput_help, type=str)
    sub_b.add_argument('-o', '--output', metavar="OUTPUT_DIR", dest='c_output', type=str, help=coutput_help)
    sub_b.add_argument('-s', '--samtools', metavar="SAMTOOLS", help=samtools_help, type=str)
    sub_b.add_argument('--scorrect', help="Singleton correction, default: True.",
                       choices=[True, False], type=bool)
    sub_b.add_argument('-b', '--bedfile', help=bedfile_help, default=bedfile, type=str)
    sub_b.add_argument('-c', '--cutoff', type=float, help="Consensus cut-off, default: 0.7 (70%% of reads must have the"
                                                          " same base to form a consensus).")
    sub_b.add_argument('--cleanup', choices=[True, False], type=bool, help=cleanup_help)
    sub_b.set_defaults(func=consensus)

    # Parse args
    args = main_p.parse_args()

    if args.subparser_name is None:
        main_p.print_help()
    else:
        if args.subparser_name == 'fastq2bam':
            # Check if required arguments provided
            if args.fastq1 is None or args.fastq2 is None or args.output is None or args.bwa is None or \
                            args.ref is None or args.samtools is None:
                sub_a.error("Command line arguments must be provided if config file is not present.\n"
                            "REQUIRED: fastq1, fastq2, output, bwa, ref, samtools, and barcode pattern OR list")
                sub_a.print_help()
            # Check if either barcode pattern or list is set. At least one must be provided.
            elif args.bpattern is None and args.blist is None:
                sub_a.error("At least one of -b or -l required.")
            # Check proper barcode design provided for barcode pattern
            elif re.findall(r'[^A|C|G|T|N]', args.bpattern):
                raise ValueError("Invalid barcode pattern containing characters other than A, C, G, T, and N.")
            # Check list for faulty barcodes in list
            elif args.blist is not None:
                blist = open(args.blist, "r").read().splitlines()
                if re.search("[^ACGTN]", "".join(blist)) is not None:
                    raise ValueError("List contains invalid barcodes. Please specify barcodes with A|C|G|T.")
                else:
                    args.func(args)
            else:
                args.func(args)
        elif args.subparser_name == 'consensus':
            if args.bam is None or args.c_output is None or args.samtools is None:
                sub_b.error("Command line arguments must be provided if config file is not present.\n"
                            "REQUIRED: input, output, and samtools.")
                sub_b.print_help()
            else:
                args.func(args)
        else:
            main_p.print_help()


