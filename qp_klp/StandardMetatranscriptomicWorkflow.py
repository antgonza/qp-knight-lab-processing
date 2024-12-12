from .Protocol import Illumina
from os.path import join, abspath, exists
from os import walk
from shutil import rmtree
from sequence_processing_pipeline.Pipeline import Pipeline
from .Assays import Metatranscriptomic
from .Assays import ASSAY_NAME_METATRANSCRIPTOMIC
from .FailedSamplesRecord import FailedSamplesRecord
from .Workflows import Workflow


class StandardMetatranscriptomicWorkflow(Workflow, Metatranscriptomic,
                                         Illumina):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.mandatory_attributes = ['qclient', 'uif_path',
                                     'lane_number', 'config_fp',
                                     'run_identifier', 'output_dir', 'job_id',
                                     'lane_number', 'is_restart']

        self.confirm_mandatory_attributes()

        # second stage initializer that could conceivably be pushed down into
        # specific children requiring specific parameters.
        self.qclient = self.kwargs['qclient']

        self.pipeline = Pipeline(self.kwargs['config_fp'],
                                 self.kwargs['run_identifier'],
                                 self.kwargs['uif_path'],
                                 self.kwargs['output_dir'],
                                 self.kwargs['job_id'],
                                 ASSAY_NAME_METATRANSCRIPTOMIC,
                                 lane_number=self.kwargs['lane_number'])

        self.fsr = FailedSamplesRecord(self.kwargs['output_dir'],
                                       self.pipeline.sample_sheet.samples)

        self.master_qiita_job_id = None

        self.lane_number = self.kwargs['lane_number']
        self.is_restart = bool(self.kwargs['is_restart'])

        if self.is_restart is True:
            self.determine_steps_to_skip()

        # this is a convenience member to allow testing w/out updating Qiita.
        self.update = True

        if 'update_qiita' in kwargs:
            if not isinstance(kwargs['update_qiita'], bool):
                raise ValueError("value for 'update_qiita' must be of "
                                 "type bool")

            self.update = kwargs['update_qiita']

    def determine_steps_to_skip(self):
        out_dir = self.pipeline.output_path

        directories_to_check = ['ConvertJob', 'NuQCJob',
                                'FastQCJob', 'GenPrepFileJob']

        for directory in directories_to_check:
            if exists(join(out_dir, directory)):
                if exists(join(out_dir, directory, 'job_completed')):
                    # this step completed successfully.
                    self.skip_steps.append(directory)
                else:
                    # work stopped before this job could be completed.
                    rmtree(join(out_dir, directory))

    def execute_pipeline(self):
        '''
        Executes steps of pipeline in proper sequence.
        :return: None
        '''
        if not self.is_restart:
            self.pre_check()

        # this is performed even in the event of a restart.
        self.generate_special_map()

        # even if a job is being skipped, it's being skipped because it was
        # determined that it already completed successfully. Hence,
        # increment the status because we are still iterating through them.

        self.update_status("Converting data", 1, 9)
        if "ConvertJob" not in self.skip_steps:
            # converting raw data to fastq depends heavily on the instrument
            # used to generate the run_directory. Hence this method is
            # supplied by the instrument mixin.
            # NB: convert_raw_to_fastq() now generates fsr on its own
            results = self.convert_raw_to_fastq()

        self.update_status("Performing quality control", 2, 9)
        if "NuQCJob" not in self.skip_steps:
            # NB: quality_control generates its own fsr
            self.quality_control(self.pipeline)

        self.update_status("Generating reports", 3, 9)
        if "FastQCJob" not in self.skip_steps:
            # reports are currently implemented by the assay mixin. This is
            # only because metaranscriptomic runs currently require a failed-
            # samples report to be generated. This is not done for amplicon
            # runs since demultiplexing occurs downstream of SPP.
            results = self.generate_reports()
            self.fsr_write(results, 'FastQCJob')

        self.update_status("Generating preps", 4, 9)
        if "GenPrepFileJob" not in self.skip_steps:
            # preps are currently associated with array mixin, but only
            # because there are currently some slight differences in how
            # FastQCJob gets instantiated(). This could get moved into a
            # shared method, but probably still in Assay.
            self.generate_prep_file()

        # moved final component of genprepfilejob outside of object.
        # obtain the paths to the prep-files generated by GenPrepFileJob
        # w/out having to recover full state.
        tmp = join(self.pipeline.output_path, 'GenPrepFileJob', 'PrepFiles')

        self.has_replicates = False

        prep_paths = []
        self.prep_file_paths = {}

        for root, dirs, files in walk(tmp):
            for _file in files:
                # breakup the prep-info-file into segments
                # (run-id, project_qid, other) and cleave
                # the qiita-id from the project_name.
                qid = _file.split('.')[1].split('_')[-1]

                if qid not in self.prep_file_paths:
                    self.prep_file_paths[qid] = []

                _path = abspath(join(root, _file))
                if _path.endswith('.tsv'):
                    prep_paths.append(_path)
                    self.prep_file_paths[qid].append(_path)

            for _dir in dirs:
                if _dir == '1':
                    # if PrepFiles contains the '1' directory, then it's a
                    # given that this sample-sheet contains replicates.
                    self.has_replicates = True

        # currently imported from Assay although it is a base method. it
        # could be imported into Workflows potentially, since it is a post-
        # processing step. All pairings of assay and instrument type need to
        # generate prep-info files in the same format.
        self.overwrite_prep_files(prep_paths)

        # for now, simply re-run any line below as if it was a new job, even
        # for a restart. functionality is idempotent, except for the
        # registration of new preps in Qiita. These will simply be removed
        # manually.

        # post-processing steps are by default associated with the Workflow
        # class, since they deal with fastq files and Qiita, and don't depend
        # on assay or instrument type.
        self.update_status("Generating sample information", 5, 9)
        self.sifs = self.generate_sifs()

        # post-processing step.
        self.update_status("Registering blanks in Qiita", 6, 9)
        if self.update:
            self.update_blanks_in_qiita()

        self.update_status("Loading preps into Qiita", 7, 9)
        if self.update:
            self.update_prep_templates()

        # before we load preps into Qiita we need to copy the fastq
        # files n times for n preps and correct the file-paths each
        # prep is pointing to.
        self.load_preps_into_qiita()

        self.update_status("Generating packaging commands", 8, 9)
        self.generate_commands()

        self.update_status("Packaging results", 9, 9)
        if self.update:
            self.execute_commands()