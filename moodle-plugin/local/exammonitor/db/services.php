<?php
// This file is part of Moodle - http://moodle.org/

defined('MOODLE_INTERNAL') || die();

$functions = [
    'local_exammonitor_get_status' => [
        'classname' => 'local_exammonitor\\external\\get_status',
        'methodname' => 'execute',
        'description' => 'Return quiz status information for a date range.',
        'type' => 'read',
        'ajax' => false,
        'capabilities' => 'local/exammonitor:access',
    ],
    'local_exammonitor_ping' => [
        'classname' => 'local_exammonitor\\external\\ping',
        'methodname' => 'execute',
        'description' => 'Lightweight health check for the Exam Monitor integration.',
        'type' => 'read',
        'ajax' => false,
        'capabilities' => 'local/exammonitor:access',
    ],
    'local_exammonitor_find_course' => [
        'classname' => 'local_exammonitor\\external\\find_course',
        'methodname' => 'execute',
        'description' => 'Find one course by exact shortname and return its quizzes.',
        'type' => 'read',
        'ajax' => false,
        'capabilities' => 'local/exammonitor:access',
    ],
    'local_exammonitor_import_xml' => [
        'classname' => 'local_exammonitor\\external\\import_xml',
        'methodname' => 'execute',
        'description' => 'Import Moodle XML from a user draft area and add the imported questions to an existing quiz.',
        'type' => 'write',
        'ajax' => false,
        'capabilities' => 'local/exammonitor:access',
    ],
];

$services = [
    'Exam Monitor Integration' => [
        'functions' => [
            'local_exammonitor_ping',
            'local_exammonitor_get_status',
            'local_exammonitor_find_course',
            'local_exammonitor_import_xml',
            'core_webservice_get_site_info',
            'core_files_get_unused_draft_itemid',
        ],
        'restrictedusers' => 1,
        'enabled' => 1,
        'shortname' => 'exammonitor',
        'requiredcapability' => 'local/exammonitor:access',
        'downloadfiles' => 0,
        'uploadfiles' => 1,
    ],
];
