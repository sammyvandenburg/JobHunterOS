"""Role families and company marks for the dashboard: recognition aids with no influence on matching."""

from __future__ import annotations

from jobhunter.services import roles


def test_titles_map_to_one_of_twenty_families_and_unknown_is_engineering():
    cases = {
        "Senior DevOps Engineer": "devops", "Site Reliability Engineer (SRE)": "sre", "Platform Engineer": "platform",
        "Infrastructure Engineer": "infrastructure", "Cloud Engineer": "cloud", "Kubernetes Engineer": "kubernetes",
        "Linux Systems Engineer": "systems", "Network Engineer": "network", "Security Engineer": "security",
        "DevSecOps Engineer": "devsecops", "Software Engineer": "software", "Backend Engineer": "backend",
        "Frontend Engineer": "frontend", "Data Engineer": "data", "Data Scientist": "datascience",
        "Machine Learning Engineer": "ml", "Solutions Architect": "architect", "Engineering Manager": "manager",
        "Technical Consultant": "consultant", "Cloud Architect": "cloud_architect", "Passenger Services Agent": "engineering",
        "Cloud Infrastructure Engineer": "infrastructure", "Platform Engineering Manager": "manager", "": "engineering",
    }
    for title, fid in cases.items():
        got = roles.role_family(title)
        assert got["id"] == fid, (title, got)
        assert got["label"] and got["tag"]
    assert len(roles.FAMILIES) == 20 and len({f[0] for f in roles.FAMILIES}) == 20


def test_company_marks_use_only_the_verified_employer_domain_or_initials():
    assert roles.company_mark("ElevenLabs", "https://elevenlabs.io/careers/123", "verified") == {"domain": "elevenlabs.io", "initials": "EL"}
    assert roles.company_mark("Northwind Cloud", "https://careers.northwind.co.uk/jobs/1", "verified")["domain"] == "northwind.co.uk"
    assert roles.company_mark("Northwind Cloud", "https://job-boards.greenhouse.io/northwind/jobs/1", "verified")["domain"] is None  # an ATS host is not the employer
    assert roles.company_mark("Northwind Cloud", "https://www.linkedin.com/jobs/view/1", "verified")["domain"] is None
    assert roles.company_mark("Northwind Cloud", "https://northwind.example/jobs/1", "unverified")["domain"] is None  # unverified: initials only
    assert roles.company_mark("Northwind Cloud", None, None) == {"domain": None, "initials": "NC"}
    assert roles.company_mark(None, "https://x.example/1", "verified", employer_confidential=True) == {"domain": None, "initials": "?"}
    assert roles.initials("Google") == "GO" and roles.initials("Deutsche Telekom AG") == "DT" and roles.initials("The Red Hat Company") == "RH"


def test_short_locations_never_dump_long_lists():
    label, places = roles.short_location("London, UK", "Remote", "Remote within the EU (the posting says: EU only). Check before you apply.")
    assert label == "London, UK · Remote within the EU" and places == ["London, UK"]
    label, places = roles.short_location("Amsterdam; Berlin; Dublin; Lisbon; Madrid; Paris; Prague; Rome; Vienna; Warsaw", "Hybrid", None)
    assert label == "Amsterdam + 9 locations · Hybrid" and len(places) == 10
    assert roles.short_location(None, None, None)[0] == "Location not stated"
    assert roles.short_location("Remote worldwide", "Remote", None)[0] == "Remote"
