<?php
namespace local_exammonitor\external;

defined('MOODLE_INTERNAL') || die();

use context_system;
use external_api;
use external_function_parameters;
use external_multiple_structure;
use external_single_structure;
use external_value;
use moodle_exception;

class find_course extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'shortname' => new external_value(PARAM_TEXT, 'Exact course shortname'),
        ]);
    }

    public static function execute(string $shortname): array {
        global $DB;
        self::validate_parameters(self::execute_parameters(), ['shortname' => $shortname]);
        require_capability('local/exammonitor:access', context_system::instance());
        $shortname = trim($shortname);
        if ($shortname === '') {
            throw new moodle_exception('invalidparameter', 'error', '', null, 'Empty shortname.');
        }
        $course = $DB->get_record('course', ['shortname' => $shortname], 'id,fullname,shortname', IGNORE_MISSING);
        if (!$course) {
            return ['found' => 0, 'id' => 0, 'fullname' => '', 'shortname' => $shortname, 'quizids' => []];
        }
        $quizids = $DB->get_fieldset_select('quiz', 'id', 'course = :courseid', ['courseid' => $course->id]);
        return [
            'found' => 1,
            'id' => (int)$course->id,
            'fullname' => (string)$course->fullname,
            'shortname' => (string)$course->shortname,
            'quizids' => array_map('intval', $quizids),
        ];
    }

    public static function execute_returns(): external_single_structure {
        return new external_single_structure([
            'found' => new external_value(PARAM_INT, '1 if found'),
            'id' => new external_value(PARAM_INT, 'Course id'),
            'fullname' => new external_value(PARAM_TEXT, 'Course full name'),
            'shortname' => new external_value(PARAM_TEXT, 'Course shortname'),
            'quizids' => new external_multiple_structure(new external_value(PARAM_INT, 'Quiz id')),
        ]);
    }
}
