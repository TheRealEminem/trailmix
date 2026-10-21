"""Builds backend/assets/recording-laws.json and docs/recording-laws.json (the same file) from the entries below.
Edit an entry here, bump "updated" (and "reviewed" on the entries you checked), then run from the repository root:
    python3 scripts/build-recording-laws.py .
The app picks up the docs/ copy within a day once it's published, without an app update."""
import json, sys
from pathlib import Path

ROOT = Path(sys.argv[1])
RCFP = "https://www.rcfp.org/reporters-recording-guide/"
REVIEWED = "2026-10"
ACROSS = " On a call with someone in a state where everyone must agree, follow that stricter rule."

ONE = "One person on the call (you) can agree to recording it. You don't need the others' permission, though telling them is good practice."
ALL = "Everyone on the call must agree before you record it. Live transcription counts too: it captures the conversation the same way."

# id: (name, consent, citation, notes)
US = {
 "AL": ("Alabama", "one", "Ala. Code § 13A-11-30", ""),
 "AK": ("Alaska", "one", "Alaska Stat. § 42.20.310", ""),
 "AZ": ("Arizona", "one", "Ariz. Rev. Stat. § 13-3005", ""),
 "AR": ("Arkansas", "one", "Ark. Code § 5-60-120", ""),
 "CA": ("California", "all", "Cal. Penal Code §§ 632, 632.7", "Applies to confidential conversations, which includes most calls and video meetings. Saying \"this call is recorded\" at the start, with people staying on, is commonly treated as consent."),
 "CO": ("Colorado", "one", "Colo. Rev. Stat. § 18-9-303", ""),
 "CT": ("Connecticut", "mixed", "Conn. Gen. Stat. §§ 53a-187, 52-570d", "Phone calls need everyone's consent (or a verbal notice at the start, or a beep); other conversations need one person's."),
 "DE": ("Delaware", "mixed", "Del. Code tit. 11, §§ 1335, 2402", "Two Delaware laws conflict: the wiretap law allows one-person consent, while the privacy law has been read to need everyone's. Treat it as everyone."),
 "DC": ("District of Columbia", "one", "D.C. Code § 23-542", ""),
 "FL": ("Florida", "all", "Fla. Stat. § 934.03", "Applies where people reasonably expect the conversation to be private."),
 "GA": ("Georgia", "one", "Ga. Code § 16-11-66", ""),
 "HI": ("Hawaii", "one", "Haw. Rev. Stat. § 803-42", "Recording inside a private place (like someone's home) has stricter rules."),
 "ID": ("Idaho", "one", "Idaho Code § 18-6702", ""),
 "IL": ("Illinois", "all", "720 ILCS 5/14-2", "Applies to private conversations: ones where people reasonably expect not to be recorded."),
 "IN": ("Indiana", "one", "Ind. Code § 35-31.5-2-176", ""),
 "IA": ("Iowa", "one", "Iowa Code § 727.8", ""),
 "KS": ("Kansas", "one", "Kan. Stat. § 21-6101", ""),
 "KY": ("Kentucky", "one", "Ky. Rev. Stat. § 526.010", ""),
 "LA": ("Louisiana", "one", "La. Rev. Stat. § 15:1303", ""),
 "ME": ("Maine", "one", "Me. Rev. Stat. tit. 15, § 709", ""),
 "MD": ("Maryland", "all", "Md. Code, Cts. & Jud. Proc. § 10-402", ""),
 "MA": ("Massachusetts", "all", "Mass. Gen. Laws ch. 272, § 99", "Recording secretly is the offence: an open recording that everyone knows about is generally allowed."),
 "MI": ("Michigan", "mixed", "Mich. Comp. Laws § 750.539c", "The law reads as everyone must agree, but courts have allowed a participant to record their own conversation. Telling everyone is the safe choice."),
 "MN": ("Minnesota", "one", "Minn. Stat. § 626A.02", ""),
 "MS": ("Mississippi", "one", "Miss. Code § 41-29-531", ""),
 "MO": ("Missouri", "one", "Mo. Rev. Stat. § 542.402", ""),
 "MT": ("Montana", "all", "Mont. Code § 45-8-213", "Exceptions include public officials doing their jobs and recordings with a warning that the conversation is recorded."),
 "NE": ("Nebraska", "one", "Neb. Rev. Stat. § 86-290", ""),
 "NV": ("Nevada", "mixed", "Nev. Rev. Stat. §§ 200.620, 200.650", "Phone and internet calls need everyone's consent (the Nevada Supreme Court said so); in-person conversations need one person's."),
 "NH": ("New Hampshire", "all", "N.H. Rev. Stat. § 570-A:2", ""),
 "NJ": ("New Jersey", "one", "N.J. Stat. § 2A:156A-4", ""),
 "NM": ("New Mexico", "one", "N.M. Stat. § 30-12-1", ""),
 "NY": ("New York", "one", "N.Y. Penal Law §§ 250.00, 250.05", ""),
 "NC": ("North Carolina", "one", "N.C. Gen. Stat. § 15A-287", ""),
 "ND": ("North Dakota", "one", "N.D. Cent. Code § 12.1-15-02", ""),
 "OH": ("Ohio", "one", "Ohio Rev. Code § 2933.52", ""),
 "OK": ("Oklahoma", "one", "Okla. Stat. tit. 13, § 176.4", ""),
 "OR": ("Oregon", "mixed", "Or. Rev. Stat. § 165.540", "Phone and video calls need one person's consent; in-person conversations need everyone to be told they're being recorded."),
 "PA": ("Pennsylvania", "all", "18 Pa. Cons. Stat. § 5704", ""),
 "RI": ("Rhode Island", "one", "R.I. Gen. Laws § 11-35-21", ""),
 "SC": ("South Carolina", "one", "S.C. Code § 17-30-30", ""),
 "SD": ("South Dakota", "one", "S.D. Codified Laws § 23A-35A-20", ""),
 "TN": ("Tennessee", "one", "Tenn. Code § 39-13-601", ""),
 "TX": ("Texas", "one", "Tex. Penal Code § 16.02", ""),
 "UT": ("Utah", "one", "Utah Code § 77-23a-4", ""),
 "VT": ("Vermont", "unknown", "No recording statute", "Vermont has no law on recording conversations; courts have treated one-person consent as enough. Telling everyone is the safe choice."),
 "VA": ("Virginia", "one", "Va. Code § 19.2-62", ""),
 "WA": ("Washington", "all", "Rev. Code Wash. § 9.73.030", "Announcing the recording at the start, so that it's itself recorded, counts as consent."),
 "WV": ("West Virginia", "one", "W. Va. Code § 62-1D-3", ""),
 "WI": ("Wisconsin", "one", "Wis. Stat. § 968.31", ""),
 "WY": ("Wyoming", "one", "Wyo. Stat. § 7-3-702", ""),
}

GDPR = ("Using recordings for work counts as processing personal data under the GDPR: you need a lawful reason, must tell "
        "people, and should keep recordings no longer than you need them. Recording purely for yourself (household use) "
        "is outside the GDPR.")
GDPR_KEEP = {"suggest": 0, "why": "Keeping only the transcript and notes, not the audio, keeps less personal data, which the GDPR asks for."}

COUNTRIES = [
 ("CA", "Canada", "one", ["Criminal Code, R.S.C. 1985, c. C-46, s. 184", "PIPEDA"], ONE,
  "Businesses must also follow privacy law (PIPEDA, or the provincial law in Quebec, Alberta and BC): tell people why you're recording and keep it only as long as needed.", None),
 ("GB", "United Kingdom", "one", ["Investigatory Powers Act 2016", "UK GDPR / Data Protection Act 2018"],
  "You can record a call you're in for your own use without telling the others. Sharing it, or recording for work, brings in data-protection law.",
  "For work, UK GDPR applies: tell people you're recording, have a lawful reason, and keep the recording only as long as you need it.",
  {"suggest": 0, "why": "Keeping only the transcript and notes, not the audio, keeps less personal data, which UK GDPR asks for."}),
 ("EU", "European Union (other countries)", "mixed", ["GDPR (Regulation (EU) 2016/679), Art. 5, 6, 13"],
  "Rules on recording a call you're in differ by country; the GDPR applies on top whenever recordings are used for work.",
  GDPR + " Italy, Spain, the Netherlands and Ireland generally let a participant record; Belgium, Portugal, Greece and Austria are stricter. Germany and France have their own entries.",
  GDPR_KEEP),
 ("DE", "Germany", "all", ["StGB § 201 (Verletzung der Vertraulichkeit des Wortes)", "GDPR"], ALL,
  "Recording the private spoken word without consent is a crime in Germany, even for a participant. " + GDPR, GDPR_KEEP),
 ("FR", "France", "all", ["Code pénal art. 226-1", "GDPR"], ALL,
  "Recording private words without the speaker's consent is a crime. Employers must also tell staff and the works council before recording calls. " + GDPR, GDPR_KEEP),
 ("CH", "Switzerland", "all", ["Swiss Criminal Code art. 179ter", "Federal Act on Data Protection (FADP)"], ALL,
  "Recording a non-public conversation you're in without the others' consent is a crime.",
  {"suggest": 0, "why": "Swiss data-protection law asks you to keep personal data no longer than needed."}),
 ("AU", "Australia", "mixed", ["Telecommunications (Interception and Access) Act 1979 (Cth)", "Surveillance Devices Act 2007 (NSW)", "Surveillance Devices Act 1999 (Vic)"],
  "It depends on the state. In New South Wales, South Australia, Western Australia and Tasmania everyone generally needs to agree; Victoria, Queensland and the Northern Territory let a participant record.",
  "The federal law on intercepting calls can also apply to phone calls. When people are in different states, follow the stricter rule.", None),
 ("NZ", "New Zealand", "one", ["Crimes Act 1961, s. 216B", "Privacy Act 2020"], ONE,
  "Businesses must also follow the Privacy Act: tell people why you're recording and keep it only as long as needed.", None),
 ("IE", "Ireland", "one", ["Interception of Postal Packets and Telecommunications Messages (Regulation) Act 1993", "GDPR"], ONE, GDPR, GDPR_KEEP),
 ("IN", "India", "unknown", ["Constitution of India, Art. 21 (K.S. Puttaswamy v. Union of India, 2017)", "Digital Personal Data Protection Act 2023"],
  "There's no law that clearly allows or forbids a participant recording a call. Courts have sometimes refused secret recordings as evidence on privacy grounds.",
  "For work, the Digital Personal Data Protection Act requires notice and consent for collecting personal data.", None),
 ("JP", "Japan", "one", ["Act on the Protection of Personal Information (APPI)"], ONE,
  "Businesses must state why they collect personal information, including recordings.", None),
 ("SG", "Singapore", "one", ["Personal Data Protection Act 2012"], ONE,
  "Organisations must tell people and get consent before recording them under the PDPA.", None),
 ("BR", "Brazil", "one", ["Lei 9.296/1996", "LGPD (Lei 13.709/2018)"], ONE,
  "Courts accept a participant's recording. For work, the LGPD requires a lawful reason and notice.", None),
 ("ZA", "South Africa", "one", ["RICA (Act 70 of 2002), s. 4", "POPIA (Act 4 of 2013)"], ONE,
  "For work, POPIA requires telling people and keeping recordings only as long as needed.", None),
 ("IL", "Israel", "one", ["Wiretap Law 5739-1979"], ONE, "", None),
 ("AE", "United Arab Emirates", "all", ["Federal Decree-Law No. 34 of 2021, art. 44", "Federal Decree-Law No. 31 of 2021 (Penal Code)"], ALL,
  "Recording or sharing someone's conversation without consent can be a crime, with heavy penalties.", None),
]

regions = []
for code, (name, consent, cite, notes) in US.items():
    summary = {"one": ONE, "all": ALL, "mixed": "It depends on the kind of conversation; see below.",
               "unknown": "There's no clear rule."}[consent] + ("" if consent == "all" else ACROSS)
    regions.append({"id": f"US-{code}", "country": "US", "name": name, "consent": consent, "summary": summary,
                    "notes": notes, "retention": None, "sources": [cite, RCFP], "reviewed": REVIEWED})
for code, name, consent, cites, summary, notes, keep in COUNTRIES:
    regions.append({"id": code, "country": code, "name": name, "consent": consent, "summary": summary,
                    "notes": notes, "retention": keep, "sources": cites, "reviewed": REVIEWED})

table = {
 "version": 1, "updated": "2026-10-20",
 "disclaimer": "General information, not legal advice. Laws change and depend on where everyone on the call is. Following them is up to you.",
 "regions": regions,
 "fallback": {"id": "OTHER", "name": "Somewhere else", "consent": "unknown",
              "summary": "We don't have an entry for where you are yet. Check the law where you and the people you record are; asking everyone first is always the safe choice.",
              "notes": "", "retention": None, "sources": [], "reviewed": REVIEWED},
}
text = json.dumps(table, indent=1, ensure_ascii=False) + "\n"
(ROOT / "backend/assets/recording-laws.json").write_text(text)
(ROOT / "docs/recording-laws.json").write_text(text)
print(len(regions), "regions")
