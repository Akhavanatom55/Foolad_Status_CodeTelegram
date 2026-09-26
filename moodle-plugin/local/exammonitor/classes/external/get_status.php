<?php
// This file is part of Moodle - http://moodle.org/
namespace local_exammonitor\external;

defined('MOODLE_INTERNAL') || die();

use context_system;
use external_api;
use external_function_parameters;
use external_multiple_structure;
use external_single_structure;
use external_value;
use moodle_exception;

class get_status extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([
            'date_start' => new external_value(PARAM_INT, 'Inclusive Unix timestamp'),
            'date_end' => new external_value(PARAM_INT, 'Exclusive Unix timestamp'),
            'datemode' => new external_value(PARAM_ALPHANUMEXT, 'open_time, created_time, or either', VALUE_DEFAULT, 'open_time'),
        ]);
    }

    public static function execute(int $date_start, int $date_end, string $datemode = 'open_time'): array {
        global $DB, $CFG;
        self::validate_parameters(self::execute_parameters(), compact('date_start', 'date_end', 'datemode'));
        require_capability('local/exammonitor:access', context_system::instance());
        if ($date_end <= $date_start || !in_array($datemode, ['open_time', 'created_time', 'either'], true)) {
            throw new moodle_exception('invalidparameter', 'error', '', null, 'Invalid date range or mode.');
        }

        $params = ['start1' => $date_start, 'end1' => $date_end];
        if ($datemode === 'open_time') {
            $where = 'q.timeopen >= :start1 AND q.timeopen < :end1 AND q.timeopen > 0';
        } elseif ($datemode === 'created_time') {
            $where = 'q.timecreated >= :start1 AND q.timecreated < :end1';
        } else {
            $where = '((q.timeopen >= :start1 AND q.timeopen < :end1 AND q.timeopen > 0) OR (q.timecreated >= :start2 AND q.timecreated < :end2))';
            $params['start2'] = $date_start;
            $params['end2'] = $date_end;
        }

        // IMPORTANT: This function used to perform 4-5 separate DB queries per Quiz,
        // including a full enrollment count. On a busy LMS that made /webservice/rest/server.php
        // run long enough for the reverse proxy to return HTTP 504. Everything that can be
        // aggregated is now fetched in a few GROUP BY queries.
        $sql = "SELECT q.id AS quizid, q.course AS courseid, q.name, q.timecreated, q.timeopen, q.timeclose,
                       q.timelimit, q.sumgrades, c.fullname AS course_fullname, c.shortname AS course_shortname,
                       cm.id AS cmid
                  FROM {quiz} q
                  JOIN {course} c ON c.id = q.course
                  JOIN {course_modules} cm ON cm.instance = q.id
                  JOIN {modules} m ON m.id = cm.module AND m.name = 'quiz'
                 WHERE cm.deletioninprogress = 0
                   AND ($where)
              ORDER BY q.timeopen ASC, q.id ASC";

        $records = $DB->get_records_sql($sql, $params);
        if (!$records) {
            return ['quizzes' => []];
        }

        $quizids = array_map('intval', array_keys($records));
        $courseids = array_values(array_unique(array_map(static function($record) {
            return (int)$record->courseid;
        }, $records)));

        [$quizinsql, $quizparams] = $DB->get_in_or_equal($quizids, SQL_PARAMS_NAMED, 'quiz');
        [$courseinsql, $courseparams] = $DB->get_in_or_equal($courseids, SQL_PARAMS_NAMED, 'course');

        $questioncounts = [];
        foreach ($DB->get_records_sql(
            "SELECT quizid, COUNT(*) AS cnt
               FROM {quiz_slots}
              WHERE quizid $quizinsql AND slot > 0 AND questionid > 0
           GROUP BY quizid",
            $quizparams
        ) as $row) {
            $questioncounts[(int)$row->quizid] = (int)$row->cnt;
        }

        $enrolledcounts = [];
        $now = time();
        $enrolparams = $courseparams + [
            'now1' => $now, 'now2' => $now, 'now3' => $now, 'now4' => $now,
        ];
        foreach ($DB->get_records_sql(
            "SELECT e.courseid, COUNT(DISTINCT ue.userid) AS cnt
               FROM {user_enrolments} ue
               JOIN {enrol} e ON e.id = ue.enrolid
              WHERE e.courseid $courseinsql
                AND ue.status = 0 AND e.status = 0
                AND (ue.timestart = 0 OR ue.timestart <= :now1)
                AND (ue.timeend = 0 OR ue.timeend >= :now2)
                AND (e.enrolstartdate = 0 OR e.enrolstartdate <= :now3)
                AND (e.enrolenddate = 0 OR e.enrolenddate >= :now4)
           GROUP BY e.courseid",
            $enrolparams
        ) as $row) {
            $enrolledcounts[(int)$row->courseid] = (int)$row->cnt;
        }

        $gradepasses = [];
        $gradeparams = $quizparams;
        foreach ($DB->get_records_sql(
            "SELECT iteminstance AS quizid, gradepass
               FROM {grade_items}
              WHERE itemtype = 'mod' AND itemmodule = 'quiz' AND itemnumber = 0
                AND iteminstance $quizinsql",
            $gradeparams
        ) as $row) {
            $gradepasses[(int)$row->quizid] = ($row->gradepass === null) ? -1.0 : (float)$row->gradepass;
        }

        $shuffle = [];
        foreach ($DB->get_records_sql(
            "SELECT quizid, MIN(shufflequestions) AS shufflequestions
               FROM {quiz_sections}
              WHERE quizid $quizinsql
           GROUP BY quizid",
            $quizparams
        ) as $row) {
            $shuffle[(int)$row->quizid] = ((int)$row->shufflequestions === 1) ? 1 : 0;
        }

        $overridecounts = [];
        foreach ($DB->get_records_sql(
            "SELECT quiz AS quizid, COUNT(*) AS cnt
               FROM {quiz_overrides}
              WHERE quiz $quizinsql
           GROUP BY quiz",
            $quizparams
        ) as $row) {
            $overridecounts[(int)$row->quizid] = (int)$row->cnt;
        }

        $out = [];
        foreach ($records as $quiz) {
            $qid = (int)$quiz->quizid;
            $cid = (int)$quiz->courseid;
            $url = !empty($CFG->wwwroot) ? rtrim($CFG->wwwroot, '/') . '/mod/quiz/view.php?id=' . (int)$quiz->cmid : '';
            $out[] = [
                'quizid' => $qid,
                'cmid' => (int)$quiz->cmid,
                'courseid' => $cid,
                'name' => (string)$quiz->name,
                'course_fullname' => (string)$quiz->course_fullname,
                'course_shortname' => (string)$quiz->course_shortname,
                'timecreated' => (int)$quiz->timecreated,
                'timeopen' => (int)$quiz->timeopen,
                'timeclose' => (int)$quiz->timeclose,
                'timelimit' => (int)$quiz->timelimit,
                'sumgrades' => (float)$quiz->sumgrades,
                'grade_pass' => $gradepasses[$qid] ?? -1.0,
                'question_count' => $questioncounts[$qid] ?? 0,
                'enrolled_count' => $enrolledcounts[$cid] ?? 0,
                'shuffle_questions' => $shuffle[$qid] ?? -1,
                'override_count' => $overridecounts[$qid] ?? 0,
                'url' => $url,
            ];
        }

        return ['quizzes' => $out];
    }

    public static function execute_returns(): external_single_structure {
        return new external_single_structure([
            'quizzes' => new external_multiple_structure(new external_single_structure([
                'quizid' => new external_value(PARAM_INT, 'Quiz id'),
                'cmid' => new external_value(PARAM_INT, 'Course module id'),
                'courseid' => new external_value(PARAM_INT, 'Course id'),
                'name' => new external_value(PARAM_TEXT, 'Quiz name'),
                'course_fullname' => new external_value(PARAM_TEXT, 'Course full name'),
                'course_shortname' => new external_value(PARAM_TEXT, 'Course shortname'),
                'timecreated' => new external_value(PARAM_INT, 'Creation timestamp'),
                'timeopen' => new external_value(PARAM_INT, 'Open timestamp'),
                'timeclose' => new external_value(PARAM_INT, 'Close timestamp'),
                'timelimit' => new external_value(PARAM_INT, 'Time limit in seconds'),
                'sumgrades' => new external_value(PARAM_FLOAT, 'Sum of question marks'),
                'grade_pass' => new external_value(PARAM_FLOAT, 'Passing grade; -1 means not set'),
                'question_count' => new external_value(PARAM_INT, 'Question count'),
                'enrolled_count' => new external_value(PARAM_INT, 'Active enrolled users'),
                'shuffle_questions' => new external_value(PARAM_INT, '1 enabled, 0 disabled, -1 unknown'),
                'override_count' => new external_value(PARAM_INT, 'Quiz override count'),
                'url' => new external_value(PARAM_RAW, 'Quiz URL'),
            ])),
        ]);
    }
}
