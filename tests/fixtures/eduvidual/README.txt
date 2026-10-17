Eduvidual fixtures - SYNTHETIC, NOT RECORDED
=============================================

Nothing in this directory was recorded from a real Eduvidual/Moodle session (no credentials were
available while developing the adapter). Every file is hand-built from the documented response
structures of Moodle (see the docstring of src/schulsync/adapters/eduvidual.py and
docs/datenzugriff_eduvidual.md for the cited sources). All names, ids, tokens and dates are invented;
tokens/sesskeys are obvious dummy values.

Real unauthenticated shapes that the files imitate (probed 2026-10-04): token_invalid_login.json /
token_service_disabled.json follow the real {"error", "errorcode", "stacktrace", "debuginfo",
"reproductionlink"} object of login/token.php.

Time reference of the tests: "now" = Wed 2026-10-14 08:00 Europe/Vienna, window = [2026-10-14 00:00,
2026-11-04 00:00). Daylight saving time ends 2026-10-25 (assignment 4102 is due before, 4103 after).

Files
  token_*.json              login/token.php answers (ok / invalid credentials / service disabled / unavailable)
  site_info*.json           core_webservice_get_site_info
  invalidtoken.json, access_exception.json   REST exception objects
  courses_*.json            core_course_get_enrolled_courses_by_timeline_classification pages
  assignments*.json         mod_assign_get_assignments (+ variants: module/course warnings, missing duedate)
  calendar_*.json           'due' events as exported by the calendar (web route); wrapped into the
                            weeks/days structure of core_calendar_get_calendar_monthly_view by the tests
  login_page.html, dashboard*.html   login form page and a logged-in / logged-out page with M.cfg
