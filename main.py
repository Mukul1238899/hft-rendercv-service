import os, subprocess, tempfile, shutil, traceback, re
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client
import typst

app = FastAPI()

supabase = create_client(
    os.environ.get("SUPABASE_URL"),
    os.environ.get("SUPABASE_SERVICE_KEY")
)

# ── Section Alias Map ─────────────────────────────────────
SECTION_ALIASES = {
    "professional_summary": [
        "summary", "profile_summary", "professional_summary",
        "about", "objective", "career_objective"
    ],
    "professional_experience": [
        "experience", "work_experience", "professional_experience",
        "employment", "career_history", "work_history"
    ],
    "core_competencies": [
        "skills", "key_skills", "core_competencies",
        "technical_skills", "competencies", "expertise",
        "core_skills", "skill_set"
    ],
    "key_achievements": [
        "achievements", "key_achievements", "key_executive_achievements",
        "accomplishments", "highlights", "awards"
    ],
    "education": [
        "education", "academic_background", "qualifications",
        "educational_background", "education_and_credentials"
    ]
}

def normalize_sections(data):
    """Normalize section names using alias map"""
    sections = data.get('cv', {}).get('sections', {})
    if not sections:
        return data

    normalized = {}
    used_keys = set()

    for standard_key, aliases in SECTION_ALIASES.items():
        for alias in aliases:
            # Check exact match
            if alias in sections and alias not in used_keys:
                normalized[standard_key] = sections[alias]
                used_keys.add(alias)
                break
            # Check case-insensitive
            for section_key in sections:
                if section_key.lower().replace(" ", "_") == alias and section_key not in used_keys:
                    normalized[standard_key] = sections[section_key]
                    used_keys.add(section_key)
                    break

    # Add any remaining sections not in alias map
    for key, val in sections.items():
        if key not in used_keys:
            normalized[key] = val

    data['cv']['sections'] = normalized
    return data

def fix_yaml(yaml_text):
    # Remove markdown backticks
    yaml_text = yaml_text.replace("```yaml", "").replace("```", "").strip()

    # Fix phone line by line
    lines = yaml_text.split("\n")
    fixed = []
    for line in lines:
        if re.match(r'\s*phone:', line):
            p = re.sub(r'[^\d+]', '', line.split(':', 1)[1])
            if p and not p.startswith('+'):
                p = '+' + p
            fixed.append(f'  phone: "{p}"')
        else:
            fixed.append(line)

    yaml_text = "\n".join(fixed)

    # Parse + normalize sections
    try:
        data = yaml.safe_load(yaml_text)
        if data and 'cv' in data:
            data = normalize_sections(data)
            yaml_text = yaml.dump(data, allow_unicode=True, default_flow_style=False)
            print("✅ Sections normalized!")
    except Exception as e:
        print(f"⚠️ YAML normalize warning: {e}")

    return yaml_text

class CVRequest(BaseModel):
    batch_id: str
    candidate_name: str
    yaml_content: str

@app.get("/health")
def health():
    return {"status": "ok", "service": "HFT RenderCV"}

@app.post("/render")
async def render_cv(req: CVRequest):
    tmpdir = tempfile.mkdtemp()
    try:
        print(f"Rendering: {req.candidate_name}")

        yaml_text = fix_yaml(req.yaml_content)
        print(f"YAML:\n{yaml_text[:400]}")

        yaml_path = os.path.join(tmpdir, "cv.yaml")
        open(yaml_path, "w").write(yaml_text)

        # Patch lib.typ
        import rendercv
        lib_src = os.path.join(os.path.dirname(rendercv.__file__), "renderer/rendercv_typst/lib.typ")
        lib_dst = os.path.join(tmpdir, "lib_local.typ")
        lib_content = open(lib_src).read().replace(
            '#import "@preview/fontawesome:0.6.0": fa-icon',
            '#let fa-icon(name, size: 1em) = []'
        )
        open(lib_dst, "w").write(lib_content)

        # Run rendercv
        output_dir = os.path.join(tmpdir, "output")
        os.makedirs(output_dir, exist_ok=True)
        result = subprocess.run(
            ["rendercv", "render", yaml_path,
             "--output-folder", output_dir,
             "--dont-generate-png"],
            capture_output=True, text=True, timeout=60
        )
        print(f"rendercv FULL: {result.stdout}")

        # Find PDF directly
        pdf_path = None
        for f in os.listdir(output_dir):
            if f.endswith(".pdf"):
                pdf_path = os.path.join(output_dir, f)
                break

        if not pdf_path:
            raise Exception(f"PDF not generated: {result.stdout[:300]}")

        print(f"PDF found: {pdf_path}")

        # Upload Supabase
        safe_name = re.sub(r'[^a-z0-9_]', '', req.candidate_name.lower().replace(" ", "_"))
        storage_path = f"cvs/{req.batch_id}/{safe_name}_cv.pdf"
        with open(pdf_path, "rb") as f:
            supabase.storage.from_("cv-files").upload(
                storage_path, f.read(),
                file_options={"content-type": "application/pdf", "upsert": "true"}
            )

        url = supabase.storage.from_("cv-files").get_public_url(storage_path)
        print(f"Done! URL: {url}")
        return {"success": True, "pdf_url": url, "candidate": req.candidate_name}

    except Exception as e:
        print(f"ERROR: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
