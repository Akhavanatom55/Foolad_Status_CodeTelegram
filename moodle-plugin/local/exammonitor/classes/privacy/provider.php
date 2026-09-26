<?php
// This file is part of Moodle - http://moodle.org/

namespace local_exammonitor\privacy;

defined('MOODLE_INTERNAL') || die();

use core_privacy\local\metadata\collection;
use core_privacy\local\request\approved_contextlist;
use core_privacy\local\request\approved_userlist;

class provider implements
    \core_privacy\local\metadata\provider,
    \core_privacy\local\request\core_userlist_provider,
    \core_privacy\local\request\plugin\provider {

    public static function get_metadata(collection $items): collection {
        $items->add_database_table('exammonitor_log', [
            'quizid' => 'privacy:metadata:local_exammonitor:quizid',
            'courseid' => 'privacy:metadata:local_exammonitor:courseid',
            'contenthash' => 'privacy:metadata:local_exammonitor:contenthash',
            'filename' => 'privacy:metadata:local_exammonitor:filename',
            'userid' => 'privacy:metadata:local_exammonitor:userid',
            'timecreated' => 'privacy:metadata:local_exammonitor:timecreated',
        ], 'privacy:metadata:local_exammonitor');
        return $items;
    }

    public static function get_contexts_for_userid(int $userid): array {
        global $DB;
        if (!$DB->record_exists('exammonitor_log', ['userid' => $userid])) {
            return [];
        }
        return [\context_system::instance()];
    }

    public static function export_user_data(approved_contextlist $contextlist): void {
        global $DB;
        if (!$contextlist->count()) {
            return;
        }
        $userid = $contextlist->get_user()->id;
        foreach ($contextlist as $context) {
            if ($context->contextlevel !== CONTEXT_SYSTEM) {
                continue;
            }
            $records = $DB->get_records('exammonitor_log', ['userid' => $userid]);
            if (!$records) {
                continue;
            }
            $writer = \core_privacy\manager::get_content_writer();
            $writer->export_related_data($context, get_string('pluginname', 'local_exammonitor'), (object)[
                'records' => array_values($records),
            ]);
        }
    }

    public static function delete_data_for_all_users_in_context(\context $context): void {
        global $DB;
        if ($context->contextlevel === CONTEXT_SYSTEM) {
            $DB->delete_records('exammonitor_log');
        }
    }

    public static function delete_data_for_user(approved_contextlist $contextlist): void {
        global $DB;
        if (!$contextlist->count()) {
            return;
        }
        $userid = $contextlist->get_user()->id;
        foreach ($contextlist as $context) {
            if ($context->contextlevel === CONTEXT_SYSTEM) {
                $DB->delete_records('exammonitor_log', ['userid' => $userid]);
                break;
            }
        }
    }

    public static function delete_data_for_users(approved_userlist $userlist): void {
        global $DB;
        $userids = $userlist->get_userids();
        if (!$userids) {
            return;
        }
        [$insql, $params] = $DB->get_in_or_equal($userids, SQL_PARAMS_NAMED);
        $DB->delete_records_select('exammonitor_log', "userid $insql", $params);
    }
}
