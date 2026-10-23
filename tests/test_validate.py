"""Resume honesty rules (spec §3.4, milestone M4: the validator rejects an injected fake skill)."""

from autopilot.match.fit_scorer import grad_year_blocker
from autopilot.models import BulletCheck, SkillGroup, TailoredBullet, TailoredEntry, VerifyResult
from autopilot.resume.tailor import tailor_resume
from autopilot.resume.validate import apply_verification, changed_bullets, check_selection, sanitize
from tests.conftest import FakeLLM, approve_all, selection


def kinds(problems):
    return sorted(p.kind for p in problems)


def test_valid_selection_passes(profile):
    assert check_selection(selection(), profile) == []


def test_injected_fake_skill_is_rejected(profile):
    sel = selection(skills=[SkillGroup(category="Tools", items=["Python", "Kubernetes"])])
    problems = check_selection(sel, profile)
    assert kinds(problems) == ["unknown_skill"]
    assert "Kubernetes" in problems[0].message
    clean, notes = sanitize(sel, profile)
    assert clean.skills[0].items == ["Python"]
    assert check_selection(clean, profile) == []


def test_invented_bullet_and_wrong_parent_are_rejected(profile):
    sel = selection(
        experience=[
            TailoredEntry(
                source_id="exp1",
                bullets=[
                    TailoredBullet(source_id="exp1.b9", text="Led a team of 10 engineers"),
                    TailoredBullet(source_id="proj1.b1", text="Built a React app"),
                ],
            )
        ]
    )
    assert kinds(check_selection(sel, profile)) == ["unknown_id", "wrong_parent"]
    clean, _ = sanitize(sel, profile)
    assert clean.experience == []


def test_new_number_is_rejected_and_reverted(profile):
    sel = selection(
        experience=[
            TailoredEntry(
                source_id="exp1",
                bullets=[
                    TailoredBullet(
                        source_id="exp1.b2", text="Wrote PostgreSQL migrations for 20 tables serving 1,000 users"
                    ),
                ],
            )
        ]
    )
    problems = check_selection(sel, profile)
    assert kinds(problems) == ["new_number"] and "1000" in problems[0].message
    clean, notes = sanitize(sel, profile)
    assert clean.experience[0].bullets[0].text == profile.bullet_index()["exp1.b2"][1].text


def test_unknown_coursework_dropped(profile):
    sel = selection()
    sel.education[0].coursework.append("Quantum Computing")
    assert kinds(check_selection(sel, profile)) == ["unknown_coursework"]
    clean, _ = sanitize(sel, profile)
    assert "Quantum Computing" not in clean.education[0].coursework


def test_verify_pass_reverts_unsupported_rewrites(profile):
    sel = selection()
    sel.experience[0].bullets[
        0
    ].text = "Architected a production FastAPI platform at scale, cutting report time from 40 minutes to 5 minutes"
    pairs = changed_bullets(sel, profile)
    assert {p["source_id"] for p in pairs} == {"exp1.b1"}
    result = VerifyResult(
        checks=[BulletCheck(source_id="exp1.b1", supported=False, problem="adds 'production' and 'at scale'")]
    )
    sel, reverted = apply_verification(sel, profile, pairs, result)
    assert sel.experience[0].bullets[0].text == profile.bullet_index()["exp1.b1"][1].text
    assert reverted[0]["problem"] == "adds 'production' and 'at scale'"


def test_rewrites_missing_from_verify_result_are_reverted(profile):
    sel = selection()
    sel.experience[0].bullets[0].text = "Something new about exp1.b1 with 40 and 5"
    pairs = changed_bullets(sel, profile)
    sel, reverted = apply_verification(sel, profile, pairs, VerifyResult(checks=[]))
    assert len(reverted) == 1


def test_tailor_retries_with_problems_then_sanitizes(profile):
    bad = selection(skills=[SkillGroup(category="Tools", items=["Kubernetes"])])
    fake = FakeLLM(tailor=[bad, bad], verify=approve_all)
    outcome = tailor_resume(
        fake,
        profile,
        {
            "company": "Acme",
            "role": "Intern",
            "term": None,
            "location": None,
            "remote": None,
            "deadline": None,
            "apply_url": "x",
            "requirements_json": "[]",
            "eligibility_json": "{}",
            "jd_text": "Python",
        },
    )
    tailor_calls = [u for t, u in fake.calls if t == "tailor"]
    assert len(tailor_calls) == 2 and "Kubernetes" in tailor_calls[1]
    assert outcome.selection.skills == []
    assert any("Kubernetes" in f for f in outcome.fixes)


def test_grad_year_blocker(profile):
    assert (
        grad_year_blocker(profile, {"grad_years": ["2026", "2027"]})
        == "Requires grad year 2026, 2027; you graduate 2028-05"
    )
    assert grad_year_blocker(profile, {"grad_years": ["December 2027 - June 2028"]}) is None
    assert grad_year_blocker(profile, {"grad_years": []}) is None


def test_placeholder_detection_ignores_real_todo_words(profile):
    assert profile.placeholder_problems() == []
    profile.projects[0].bullets[0].text = "Built a TODO app with React"
    assert profile.placeholder_problems() == []
    profile.basics.name = "TODO Full Name"
    profile.basics.links.github = "https://github.com/TODO"
    assert profile.placeholder_problems() == ["profile.yaml still has 2 TODO placeholder(s)"]


def test_intern_summary_never_claims_years(profile):
    from autopilot.resume.validate import is_internship

    sel = selection(summary="Software engineer with **4.5 years** of experience building **Python** services.")
    problems = [p.message for p in check_selection(sel, profile, internship=True)]
    assert any("years of experience" in m for m in problems)
    # For a full-time role, only the unknown number (4.5 isn't in this test profile) is flagged.
    assert all("years of experience" not in p.message for p in check_selection(sel, profile, internship=False))
    profile.summary = "Computer Science student at Texas A&M who builds Python services."
    clean, notes = sanitize(sel, profile, internship=True)
    assert clean.summary == profile.summary and "replaced the summary" in notes[0]
    assert is_internship({"role": "Software Engineering Intern", "term": None})
    assert is_internship({"role": "SWE", "term": "Summer 2027 Co-op"})
    assert not is_internship({"role": "Software Engineer", "term": None})


def test_summary_numbers_must_exist_in_profile(profile):
    sel = selection(summary="Built services used by **2M** users.")
    assert "2" in check_selection(sel, profile)[0].message


def test_leadership_bullets_are_validated(profile):
    from autopilot.models import Bullet

    profile.leadership = [Bullet(id="lead.b1", text="Mentored 3 interns")]
    ok = selection(leadership=[TailoredBullet(source_id="lead.b1", text="Mentored 3 interns")])
    assert check_selection(ok, profile) == []
    bad = selection(
        leadership=[
            TailoredBullet(source_id="lead.b1", text="Mentored 30 interns"),
            TailoredBullet(source_id="lead.b9", text="Led a team"),
        ]
    )
    assert kinds(check_selection(bad, profile)) == ["new_number", "unknown_id"]
    clean, _ = sanitize(bad, profile)
    assert [b.text for b in clean.leadership] == ["Mentored 3 interns"]


def test_resume_never_mentions_work_authorization(profile):
    for text in (
        "F-1 student who will need **H-1B** sponsorship.",
        "Eligible for CPT and OPT.",
        "US citizen engineer.",
    ):
        problems = check_selection(selection(summary=text), profile)
        assert any("work-authorization" in p.message for p in problems), text
    # Ordinary words that merely contain these letters are fine.
    assert check_selection(selection(summary="Optimized Python services at Texas A&M."), profile) == []


def test_last_position_and_optional_bullets(profile):
    from autopilot.resume.render import drop_one, optional_bullet_ids

    b1 = profile.bullet_index()["exp1.b1"][1]
    b1.position, b1.optional = "last", True
    sel = selection()  # exp1.b1 is listed first by "Claude"
    clean, _ = sanitize(sel, profile)
    assert [b.source_id for b in clean.experience[0].bullets] == ["exp1.b2", "exp1.b1"]
    # When the page is full, the optional bullet is the first thing trimmed.
    assert drop_one(clean, optional_bullet_ids(profile)) == "exp1.b1"


def test_coursework_matches_without_term_notes(profile):
    profile.education[0].coursework.append("Artificial Intelligence (Fall 2026)")
    sel = selection()
    sel.education[0].coursework = ["artificial intelligence", "Databases"]
    assert check_selection(sel, profile) == []
    clean, _ = sanitize(sel, profile)
    assert clean.education[0].coursework == ["Artificial Intelligence (Fall 2026)", "Databases"]


def test_page_fitting_keeps_relevant_projects_before_extra_job_bullets(profile):
    from autopilot.resume.render import drop_one

    sel = selection(
        experience=[
            TailoredEntry(source_id="exp1", bullets=[TailoredBullet(source_id=f"e{i}", text="x") for i in range(6)])
        ],
        projects=[
            TailoredEntry(source_id="proj1", bullets=[TailoredBullet(source_id=f"p{i}", text="x") for i in range(3)])
        ],
    )
    dropped = [drop_one(sel) for _ in range(3)]
    assert dropped == ["e5", "e4", "p2"]  # main job trimmed to 4 first, then the project's 3rd bullet


def test_street_address_is_only_for_application_forms(profile):
    from autopilot.models import Address

    profile.basics.address = Address(line1="1 Main St", city="Springfield", state="Illinois")
    assert "1 Main St" in profile.to_yaml()  # form filling
    assert "1 Main St" not in profile.to_yaml(exclude={"eeo", "answers", "work_authorization"}, include_address=False)
