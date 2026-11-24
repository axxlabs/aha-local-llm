# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
import datetime as dt

from aha_local.dates import note

TUESDAY = dt.date(2026, 11, 24)


def test_the_weekday_isnt_joined_with_a_number_from_elsewhere():
    """Seen: "Tuesday, 2pm, for 2 people" on Tuesday 24 Nov booked as "Tuesday 2 December" (a Wednesday)."""
    line = note("Book a table on flatiron KingsX, Tuesday, 2pm, for 2 people.", TUESDAY)
    assert "“Tuesday” = 24/11/2026 (today), or 01/12/2026 (the next one)" in line
    assert "02/12" not in line


def test_days_and_dates_are_worked_out_as_full_dates():
    assert "“Friday” = 27/11/2026 (the one after: 04/12/2026)" in note("Friday at 8", TUESDAY)
    assert "“next Friday” = 27/11/2026, or 04/12/2026 if they mean the week after" in note("next Friday", TUESDAY)
    assert "“tomorrow” = 25/11/2026" in note("tomorrow 9am", TUESDAY)
    assert "“the 2nd” = 02/12/2026" in note("on the 2nd at 1pm", TUESDAY)
    assert "“2 Dec” = 02/12/2026" in note("2 Dec at 2pm", TUESDAY)
    assert "26/11/2026" in note("reservar quinta-feira 20h", TUESDAY)
    assert "28/11/2026" in note("Sat 7pm", TUESDAY)
    assert "“this weekend” = 28/11/2026 to 29/11/2026" in note("this weekend", TUESDAY)
    assert "“next week” = 30/11/2026 to 06/12/2026" in note("next week", TUESDAY)


def test_ordinary_words_and_numbers_arent_dates():
    for text in ("I sat down", "book for 2 at 2pm", "2 out of 5 stars", "we may go", "the sun is out"):
        assert note(text, TUESDAY) == "", text


def test_a_wrong_weekday_in_an_answer_is_fixed_in_code():
    """Seen: "Sat 29 Nov" for Sunday 29 November, though the calendar tool gave "Sun"."""
    from aha_local.checks import fix_weekdays

    assert fix_weekdays("**Sat 29 Nov** — Sync with Natalia", TUESDAY) == ("**Sun 29 Nov** — Sync with Natalia", 1)
    assert fix_weekdays("Saturday, 29 November 2026", TUESDAY)[0] == "Sunday, 29 November 2026"
    assert fix_weekdays("Tuesday, December 2, 2026", TUESDAY)[0] == "Wednesday, December 2, 2026"
    assert fix_weekdays("Wed 25 Nov and Thurs 3 Dec", TUESDAY) == ("Wed 25 Nov and Thurs 3 Dec", 0)
    assert fix_weekdays("Fri 1 Jan", dt.date(2026, 12, 20))[0] == "Fri 1 Jan"  # 1 Jan 2027, across the year


def test_talk_about_a_checked_draft_is_dropped():
    """Seen: "The earlier times like 09:00, 12:00, 13:00 were guesses — I removed them" reached the person."""
    from aha_local.agent import finish

    answer = (
        "Here's your week:\n\n- **Sun 29 Nov** — Sync (18:00–19:00)\n\n"
        "The earlier times like 09:00, 12:00, 13:00 were guesses — I removed them from the answer above."
    )
    text, passed = finish(answer, ["Sun 2026-11-29 18:00 to 19:00"], retried=True, today=TUESDAY)
    assert "guesses" not in text and "12:00" not in text and passed
    assert "Sync (18:00–19:00)" in text


def test_answers_write_no_weekdays():
    """The model writes full dates; the app shows the weekday (SPEC §3.5.1). Quotes keep theirs."""
    from aha_local import checks

    assert checks.writes_weekday("Sync with Natalia on Sun 29/11/2026")
    assert checks.writes_weekday("See you on Saturday")
    assert not checks.writes_weekday("Sync with Natalia on 29/11/2026 at 17:00")
    assert not checks.writes_weekday('Natalia said: "Sunday works"')
    assert checks.fix_weekdays("on Sat 29/11/2026 at 17:00", TUESDAY) == ("on 29/11/2026 at 17:00", 1)
