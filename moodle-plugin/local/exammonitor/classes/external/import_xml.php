<?php
namespace local_exammonitor\external;

defined('MOODLE_INTERNAL') || die();

use context_module;
use context_system;
use context_user;
use external_api;
use external_function_parameters;
use external_single_structure;
use external_value;
use moodle_exception;
use stored_file;

class import_xml extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'quizid' => new external_value(PARAM_INT, 'Quiz instance id'),
            'course_shortname' => new external_value(PARAM_TEXT, 'Expected course shortname'),
            'draftitemid' => new external_value(PARAM_INT, 'User draft item id'),
            'filename' => new external_value(PARAM_FILE, 'XML filename'),
        ]);
    }

    public static function execute(int $quizid, string $course_shortname, int $draftitemid, string $filename): array {
        global $DB, $USER, $CFG;
        self::validate_parameters(self::execute_parameters(), compact('quizid', 'course_shortname', 'draftitemid', 'filename'));
        require_capability('local/exammonitor:access', context_system::instance());
        require_once($CFG->dirroot . '/mod/quiz/locallib.php');
        require_once($CFG->dirroot . '/question/editlib.php');
        require_once($CFG->dirroot . '/question/format.php');
        require_once($CFG->dirroot . '/question/format/xml/format.php');

        $quiz = $DB->get_record('quiz', ['id' => $quizid], '*', MUST_EXIST);
        $course = $DB->get_record('course', ['id' => $quiz->course], 'id,fullname,shortname', MUST_EXIST);
        $cm = get_coursemodule_from_instance('quiz', $quizid, $course->id, false, MUST_EXIST);
        $cmcontext = context_module::instance($cm->id);
        require_capability('mod/quiz:manage', $cmcontext);

        if (trim($course_shortname) !== (string)$course->shortname) {
            throw new moodle_exception('invalidparameter', 'error', '', null, 'Course shortname does not match quiz course.');
        }
        $filename = trim(clean_param($filename, PARAM_FILE));
        if ($filename === '' || strtolower(pathinfo($filename, PATHINFO_EXTENSION)) !== 'xml') {
            throw new moodle_exception('invalidparameter', 'error', '', null, 'Only .xml is accepted.');
        }

        $usercontext = context_user::instance($USER->id);
        $fs = get_file_storage();
        $files = $fs->get_area_files($usercontext->id, 'user', 'draft', $draftitemid, 'filename', false);
        $xmlfile = self::pick_xml_file($files, $filename);
        if (!$xmlfile) {
            throw new moodle_exception('filenotfound', 'error', '', null, 'XML file was not found in the draft area.');
        }

        $contenthash = $xmlfile->get_contenthash();
        $transaction = $DB->start_delegated_transaction();
        $tmpfile = null;
        try {
            // Prevent the most common duplicate race by locking the quiz record in the transaction.
            $DB->get_record_sql('SELECT id FROM {quiz} WHERE id = :id FOR UPDATE', ['id' => $quizid], MUST_EXIST);
            if ($DB->record_exists_select('exammonitor_log', 'quizid = :quizid AND contenthash = :hash', ['quizid' => $quizid, 'hash' => $contenthash])) {
                throw new moodle_exception('invalidparameter', 'error', '', null, 'This exact XML has already been imported into this quiz.');
            }

            raise_memory_limit(MEMORY_EXTRA);
            \core_php_time_limit::raise();
            $tmpfile = tempnam(make_temp_directory(), 'exammonitor_xml_');
            if ($tmpfile === false || !$xmlfile->copy_content_to($tmpfile)) {
                throw new moodle_exception('cannotreadfile', 'error');
            }

            $format = new \qformat_xml();
            $category = question_get_default_category($cmcontext->id, true);
            $format->setCategory($category);
            $format->setCourse($course);
            $format->setFilename($tmpfile);
            $format->setRealfilename($filename);
            $format->setCatfromfile(false);
            $format->setContextfromfile(false);
            $format->setStoponerror(true);
            $format->set_display_progress(false);

            if (!$format->importpreprocess()) {
                throw new moodle_exception('errorprocessing', 'question', '', null, 'Moodle XML preprocessing failed.');
            }
            if (!$format->importprocess()) {
                throw new moodle_exception('errorprocessing', 'question', '', null, 'Moodle XML import failed.');
            }
            if (!$format->importpostprocess()) {
                throw new moodle_exception('errorprocessing', 'question', '', null, 'Moodle XML postprocessing failed.');
            }

            $questionids = array_values(array_unique(array_filter(array_map('intval', (array)$format->questionids))));
            if (!$questionids) {
                throw new moodle_exception('noquestions', 'question');
            }
            shuffle($questionids);

            $freshquiz = $DB->get_record('quiz', ['id' => $quizid], '*', MUST_EXIST);
            $addedcount = 0;
            foreach ($questionids as $questionid) {
                // Moodle's own quiz API maintains quiz_slots and related event/cache bookkeeping.
                $result = quiz_add_quiz_question($questionid, $freshquiz, 0, null);
                if ($result === false) {
                    throw new moodle_exception('errorprocessing', 'question', '', null, 'Could not add question ' . $questionid . ' to quiz.');
                }
                $addedcount++;
            }

            $DB->set_field('quiz_sections', 'shufflequestions', 1, ['quizid' => $quizid]);
            $quizsettings = \mod_quiz\quiz_settings::create($quizid);
            $quizsettings->get_grade_calculator()->recompute_quiz_sumgrades();

            $DB->insert_record('exammonitor_log', (object)[
                'quizid' => $quizid,
                'courseid' => $course->id,
                'contenthash' => $contenthash,
                'filename' => $filename,
                'questioncount' => count($questionids),
                'addedcount' => $addedcount,
                'userid' => $USER->id,
                'timecreated' => time(),
            ]);
            $fs->delete_area_files($usercontext->id, 'user', 'draft', $draftitemid);
            $transaction->allow_commit();

            return [
                'success' => 1,
                'imported_count' => count($questionids),
                'added_count' => $addedcount,
                'category_name' => (string)$category->name,
                'shuffle_questions_enabled' => 1,
                'content_hash' => $contenthash,
                'message' => 'Imported successfully.',
            ];
        } catch (\Throwable $e) {
            throw $e;
        } finally {
            if ($tmpfile && is_file($tmpfile)) {
                @unlink($tmpfile);
            }
        }
    }

    private static function pick_xml_file(array $files, string $filename): ?stored_file {
        foreach ($files as $file) {
            if ($file->is_directory()) {
                continue;
            }
            if ($file->get_filename() === $filename) {
                return $file;
            }
        }
        foreach ($files as $file) {
            if (!$file->is_directory() && strtolower(pathinfo($file->get_filename(), PATHINFO_EXTENSION)) === 'xml') {
                return $file;
            }
        }
        return null;
    }

    public static function execute_returns(): external_single_structure {
        return new external_single_structure([
            'success' => new external_value(PARAM_INT, '1 on success'),
            'imported_count' => new external_value(PARAM_INT, 'Imported questions'),
            'added_count' => new external_value(PARAM_INT, 'Questions added to quiz'),
            'category_name' => new external_value(PARAM_TEXT, 'Question category'),
            'shuffle_questions_enabled' => new external_value(PARAM_INT, '1 when enabled'),
            'content_hash' => new external_value(PARAM_ALPHANUMEXT, 'SHA-256 hash'),
            'message' => new external_value(PARAM_TEXT, 'Result message'),
        ]);
    }
}
