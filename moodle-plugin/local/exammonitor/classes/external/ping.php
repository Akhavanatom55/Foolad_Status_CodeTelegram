<?php
// This file is part of Moodle - http://moodle.org/
namespace local_exammonitor\external;

defined('MOODLE_INTERNAL') || die();

use context_system;
use external_api;
use external_function_parameters;
use external_single_structure;
use external_value;

/**
 * Lightweight health check for the Exam Monitor integration.
 *
 * This deliberately avoids the heavier core_webservice_get_site_info call so
 * the bot can distinguish "plugin/API is alive" from unrelated site-info
 * failures. A minimal course lookup also confirms Moodle can reach its DB.
 */
class ping extends external_api {
    public static function execute_parameters(): external_function_parameters {
        return new external_function_parameters([]);
    }

    public static function execute(): array {
        global $DB;
        self::validate_parameters(self::execute_parameters(), []);
        require_capability('local/exammonitor:access', context_system::instance());

        // One tiny indexed lookup: validates the DB connection without scanning
        // courses or any large table.
        $DB->get_field('course', 'id', ['id' => SITEID], MUST_EXIST);

        global $CFG;
        return [
            'ok' => true,
            'plugin' => 'local_exammonitor',
            'version' => '1.0.2',
            'version_code' => 2026092502,
            'moodle_release' => (string)$CFG->release,
            'server_time' => time(),
            'db_ok' => true,
        ];
    }

    public static function execute_returns(): external_single_structure {
        return new external_single_structure([
            'ok' => new external_value(PARAM_BOOL, 'Health check result'),
            'plugin' => new external_value(PARAM_ALPHANUMEXT, 'Plugin component'),
            'version' => new external_value(PARAM_TEXT, 'Plugin version'),
            'version_code' => new external_value(PARAM_INT, 'Plugin version code'),
            'moodle_release' => new external_value(PARAM_TEXT, 'Moodle release'),
            'server_time' => new external_value(PARAM_INT, 'Moodle server Unix timestamp'),
            'db_ok' => new external_value(PARAM_BOOL, 'Whether the minimal DB check succeeded'),
        ]);
    }
}
