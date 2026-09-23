import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import yaml

from django.conf import settings
from django.utils import timezone

from judge.models import Language, Problem, ProblemData, ProblemGroup, ProblemTestCase


def latex_to_dmoj_markdown(tex: str) -> str:
    if not tex:
        return ""

    # 1. Remove images per instruction ("Còn đề bài có ảnh thì bỏ qua")
    tex = re.sub(r'\\includegraphics(?:\[.*?\])?\{.*?\}', '', tex)
    tex = re.sub(r'\\begin\{figure\}.*?\\end\{figure\}', '', tex, flags=re.DOTALL)
    tex = re.sub(r'<img[^>]*>', '', tex)

    # 2. Math formulas
    # Display math: \[...\] -> $$...$$, keep existing $$...$$ as block math
    tex = re.sub(r'\\\[(.*?)\\\]', r'$$\1$$', tex, flags=re.DOTALL)
    # Inline math: \(...\) -> ~...~, $...$ (not $$) -> ~...~
    tex = re.sub(r'\\\((.*?)\\\)', r'~\1~', tex, flags=re.DOTALL)
    tex = re.sub(r'(?<!\$)\$([^\$]+?)\$(?!\$)', r'~\1~', tex)

    # 3. Text formatting: bold, italic, code
    tex = re.sub(r'\\textbf\{([^}]+)\}', r'**\1**', tex)
    tex = re.sub(r'\\bf\{([^}]+)\}', r'**\1**', tex)
    tex = re.sub(r'\\bf\s+([^\\n\r]+)', r'**\1**', tex)
    tex = re.sub(r'\{\\bf\s+([^}]+)\}', r'**\1**', tex)

    tex = re.sub(r'\\textit\{([^}]+)\}', r'*\1*', tex)
    tex = re.sub(r'\\emph\{([^}]+)\}', r'*\1*', tex)
    tex = re.sub(r'\\it\{([^}]+)\}', r'*\1*', tex)
    tex = re.sub(r'\{\\it\s+([^}]+)\}', r'*\1*', tex)

    tex = re.sub(r'\\texttt\{([^}]+)\}', r'`\1`', tex)
    tex = re.sub(r'\\tt\{([^}]+)\}', r'`\1`', tex)
    tex = re.sub(r'\{\\tt\s+([^}]+)\}', r'`\1`', tex)

    # Clean LaTeX quotes: ``...'' -> "...", `...' -> '...'
    tex = re.sub(r'``([^"]*?)\'\'', r'"\1"', tex)
    tex = re.sub(r'`([^`\']*?)\'', r"'\1'", tex)

    # 4. List environments
    tex = re.sub(r'\\begin\{itemize\}', '', tex)
    tex = re.sub(r'\\end\{itemize\}', '', tex)
    tex = re.sub(r'\\begin\{enumerate\}', '', tex)
    tex = re.sub(r'\\end\{enumerate\}', '', tex)
    tex = re.sub(r'\\item\s*', r'- ', tex)

    # 5. Tabular / Center / formatting environments
    tex = re.sub(r'\\begin\{center\}', '', tex)
    tex = re.sub(r'\\end\{center\}', '', tex)
    tex = re.sub(r'\\begin\{tabular\}\{[^}]*\}', '', tex)
    tex = re.sub(r'\\end\{tabular\}', '', tex)
    tex = re.sub(r'\\hline', '', tex)
    tex = re.sub(r'\\\\', '\n', tex)

    # 6. Escapes
    tex = tex.replace(r'\%', '%')
    tex = tex.replace(r'\&', '&')
    tex = tex.replace(r'\#', '#')

    # 7. Clean up redundant blank lines
    lines = [l.strip() for l in tex.splitlines()]
    result = []
    for l in lines:
        if l or (result and result[-1]):
            result.append(l)
    return '\n'.join(result).strip()


def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def clean_problem_code(raw_code: str, fallback: str = "problem") -> str:
    cleaned = re.sub(r'[^a-z0-9_]', '', raw_code.lower())[:100]
    if not cleaned:
        cleaned = re.sub(r'[^a-z0-9_]', '', fallback.lower())[:100]
    if not cleaned:
        cleaned = "prob" + str(int(timezone.now().timestamp()))[-6:]
    return cleaned[:100]


def import_polygon_package(zip_file, code_override=None, name_override=None,
                           points_override=None, time_limit_override=None,
                           memory_limit_override=None, is_public=True, author_profile=None):
    """
    Imports a problem from a Codeforces Polygon package zip archive into DMOJ.
    Supports packages with 100+ tests, automatic .inp/.out naming (matching aplusb/test_data),
    solution.cpp compilation and execution for missing outputs, LaTeX to DMOJ markdown (~ math delimiters),
    and full DMOJ test_data editor integration.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Extract archive
        if hasattr(zip_file, 'read'):
            with zipfile.ZipFile(zip_file) as z:
                z.extractall(tmpdir)
        else:
            with zipfile.ZipFile(str(zip_file)) as z:
                z.extractall(tmpdir)

        # 1. Parse problem.xml
        xml_path = os.path.join(tmpdir, "problem.xml")
        if not os.path.exists(xml_path):
            raise ValueError("problem.xml not found in Polygon package root.")

        xml_root = ET.parse(xml_path).getroot()
        short_name = xml_root.attrib.get("short-name", "")

        # Problem name
        name_el = xml_root.find(".//names/name")
        detected_name = name_el.attrib.get("value", short_name) if name_el is not None else short_name
        problem_name = name_override.strip() if name_override else detected_name

        # Judging specs (file I/O)
        judging = xml_root.find(".//judging")
        inp_file_attr = judging.attrib.get("input-file", "") if judging is not None else ""
        out_file_attr = judging.attrib.get("output-file", "") if judging is not None else ""

        # Time and memory limits
        tl_el = xml_root.find(".//testset/time-limit")
        detected_tl = float(tl_el.text) / 1000.0 if tl_el is not None else 1.0
        time_limit = float(time_limit_override) if time_limit_override else detected_tl

        ml_el = xml_root.find(".//testset/memory-limit")
        detected_ml = int(ml_el.text) // 1024 if ml_el is not None else 262144
        if memory_limit_override:
            val = int(memory_limit_override)
            # Form takes MB (e.g. 256), DB stores KB (262144)
            memory_limit = val * 1024 if val <= 2048 else val
        else:
            memory_limit = detected_ml

        # Problem Code determination
        if code_override and code_override.strip():
            problem_code = clean_problem_code(code_override.strip())
        elif short_name and clean_problem_code(short_name) != "problem":
            problem_code = clean_problem_code(short_name)
        elif inp_file_attr and '.' in inp_file_attr:
            prefix = inp_file_attr.split('.')[0]
            problem_code = clean_problem_code(prefix, fallback=short_name)
        else:
            problem_code = clean_problem_code(short_name or "problem")

        # 2. Extract and convert problem statement
        desc_parts = []

        sections_dir = os.path.join(tmpdir, "statement-sections")
        lang_sub = None
        if os.path.exists(sections_dir):
            langs = [d for d in os.listdir(sections_dir) if os.path.isdir(os.path.join(sections_dir, d))]
            if "vietnamese" in langs:
                lang_sub = "vietnamese"
            elif "english" in langs:
                lang_sub = "english"
            elif langs:
                lang_sub = langs[0]

        legend_raw = ""
        input_raw = ""
        output_raw = ""
        notes_raw = ""
        examples = []

        if lang_sub:
            cur_sec = os.path.join(sections_dir, lang_sub)
            if os.path.exists(os.path.join(cur_sec, "legend.tex")):
                with open(os.path.join(cur_sec, "legend.tex"), encoding="utf-8", errors="replace") as f:
                    legend_raw = f.read()
            if os.path.exists(os.path.join(cur_sec, "input.tex")):
                with open(os.path.join(cur_sec, "input.tex"), encoding="utf-8", errors="replace") as f:
                    input_raw = f.read()
            if os.path.exists(os.path.join(cur_sec, "output.tex")):
                with open(os.path.join(cur_sec, "output.tex"), encoding="utf-8", errors="replace") as f:
                    output_raw = f.read()
            if os.path.exists(os.path.join(cur_sec, "notes.tex")):
                with open(os.path.join(cur_sec, "notes.tex"), encoding="utf-8", errors="replace") as f:
                    notes_raw = f.read()

            ex_inputs = sorted([f for f in os.listdir(cur_sec) if f.startswith("example.") and not f.endswith(".a")])
            for ex in ex_inputs:
                ex_out = ex + ".a"
                in_content = ""
                out_content = ""
                with open(os.path.join(cur_sec, ex), encoding="utf-8", errors="replace") as f:
                    in_content = f.read().strip()
                if os.path.exists(os.path.join(cur_sec, ex_out)):
                    with open(os.path.join(cur_sec, ex_out), encoding="utf-8", errors="replace") as f:
                        out_content = f.read().strip()
                examples.append((in_content, out_content))

        # Fallback to statements/english/problem.tex
        if not legend_raw:
            prob_tex = os.path.join(tmpdir, "statements", "english", "problem.tex")
            if os.path.exists(prob_tex):
                with open(prob_tex, encoding="utf-8", errors="replace") as f:
                    raw_all = f.read()
                parts = re.split(r'\\(InputFile|OutputFile|Example|Note)', raw_all)
                if len(parts) >= 1:
                    leg = re.sub(r'\\begin\{problem\}\{[^}]*\}\{[^}]*\}\{[^}]*\}\{[^}]*\}\{[^}]*\}', '', parts[0])
                    legend_raw = leg.strip()
                for i in range(1, len(parts), 2):
                    sec_name = parts[i]
                    sec_body = parts[i + 1] if i + 1 < len(parts) else ""
                    if sec_name == "InputFile":
                        input_raw = sec_body.strip()
                    elif sec_name == "OutputFile":
                        output_raw = sec_body.strip()
                    elif sec_name == "Note":
                        sec_body = re.sub(r'\\end\{problem\}', '', sec_body)
                        notes_raw = sec_body.strip()

        if legend_raw:
            desc_parts.append(latex_to_dmoj_markdown(legend_raw))
        if input_raw:
            desc_parts.append("### Dữ liệu vào\n" + latex_to_dmoj_markdown(input_raw))
        if output_raw:
            desc_parts.append("### Dữ liệu ra\n" + latex_to_dmoj_markdown(output_raw))

        if examples:
            ex_md = ["### Ví dụ"]
            for idx, (ex_in, ex_out) in enumerate(examples, 1):
                ex_md.append(f"#### Ví dụ {idx}")
                ex_md.append(f"**Đầu vào:**\n```\n{ex_in}\n```")
                ex_md.append(f"**Đầu ra:**\n```\n{ex_out}\n```")
            desc_parts.append('\n\n'.join(ex_md))

        if notes_raw:
            desc_parts.append("### Giải thích / Giới hạn\n" + latex_to_dmoj_markdown(notes_raw))

        final_description = '\n\n'.join(desc_parts)

        # 3. Process Testcases and Outputs
        tests_dir = os.path.join(tmpdir, "tests")
        test_inputs = []
        if os.path.exists(tests_dir):
            test_inputs = sorted([
                f for f in os.listdir(tests_dir)
                if not f.endswith(".a") and not f.endswith(".out") and not f.endswith(".ans") and not os.path.isdir(os.path.join(tests_dir, f))
            ], key=lambda x: int(x) if x.isdigit() else natural_sort_key(x))

        missing_outputs = []
        test_pairs = []
        for tf in test_inputs:
            in_path = os.path.join(tests_dir, tf)
            ans_candidates = [
                os.path.join(tests_dir, tf + ".a"),
                os.path.join(tests_dir, tf + ".out"),
                os.path.join(tests_dir, tf + ".ans"),
            ]
            found_out = next((p for p in ans_candidates if os.path.exists(p)), None)
            if found_out:
                test_pairs.append((in_path, found_out))
            else:
                missing_outputs.append(in_path)

        # If any output is missing, compile solution.cpp and run tests
        if missing_outputs:
            sol_file = None
            main_sol = xml_root.find(".//solutions/solution[@tag='main']/source")
            if main_sol is not None:
                p = os.path.join(tmpdir, main_sol.attrib.get("path", ""))
                if os.path.exists(p):
                    sol_file = p

            if not sol_file and os.path.exists(os.path.join(tmpdir, "solutions")):
                for f in sorted(os.listdir(os.path.join(tmpdir, "solutions"))):
                    if f.endswith(".cpp") and not f.startswith("check"):
                        sol_file = os.path.join(tmpdir, "solutions", f)
                        break

            if not sol_file:
                for f in sorted(os.listdir(tmpdir)):
                    if f.endswith(".cpp") and not f.startswith("check"):
                        sol_file = os.path.join(tmpdir, f)
                        break

            if not sol_file:
                raise ValueError("No output files (.a/.out) found and no solution.cpp available to generate answers.")

            exe_path = os.path.join(tmpdir, "solution_runner")
            compile_cmd = ["g++", "-O3", "-std=c++17", sol_file, "-o", exe_path]
            comp_res = subprocess.run(compile_cmd, capture_output=True, text=True)
            if comp_res.returncode != 0:
                raise RuntimeError(f"Failed to compile solution {os.path.basename(sol_file)}:\n{comp_res.stderr}")

            # Build all possible casing variants for input and output files
            possible_in_names = set()
            possible_out_names = set()

            if inp_file_attr:
                possible_in_names.add(inp_file_attr)
                possible_in_names.add(inp_file_attr.lower())
                possible_in_names.add(inp_file_attr.upper())
                stem, ext = os.path.splitext(inp_file_attr)
                possible_in_names.add(f"{stem.upper()}{ext.lower()}")
                possible_in_names.add(f"{stem.lower()}{ext.upper()}")

            if out_file_attr:
                possible_out_names.add(out_file_attr)
                possible_out_names.add(out_file_attr.lower())
                possible_out_names.add(out_file_attr.upper())
                stem, ext = os.path.splitext(out_file_attr)
                possible_out_names.add(f"{stem.upper()}{ext.lower()}")
                possible_out_names.add(f"{stem.lower()}{ext.upper()}")

            possible_in_names.update([
                f"{problem_code.upper()}.INP", f"{problem_code.lower()}.inp",
                f"{problem_code.upper()}.inp", "input.txt", "INPUT.TXT"
            ])
            possible_out_names.update([
                f"{problem_code.upper()}.OUT", f"{problem_code.lower()}.out",
                f"{problem_code.upper()}.out", "output.txt", "OUTPUT.TXT"
            ])

            # Run solution on each testcase
            run_dir = tempfile.mkdtemp(prefix="sol_run_")
            try:
                for in_path in missing_outputs:
                    with open(in_path, "r", encoding="utf-8", errors="replace") as fin:
                        inp_content = fin.read()

                    # Provide input files in all common casings for solutions using freopen/fopen
                    for fn in possible_in_names:
                        try:
                            with open(os.path.join(run_dir, fn), "w", encoding="utf-8") as fin_file:
                                fin_file.write(inp_content)
                        except Exception:
                            pass

                    # Clean any existing output files in run_dir before execution
                    for fn in os.listdir(run_dir):
                        if fn.endswith((".out", ".ans", ".OUT", ".ANS")) or fn in possible_out_names:
                            try:
                                os.remove(os.path.join(run_dir, fn))
                            except Exception:
                                pass

                    proc = subprocess.run(
                        [exe_path],
                        input=inp_content,
                        capture_output=True,
                        text=True,
                        cwd=run_dir,
                        timeout=10,
                    )

                    out_content = ""
                    # 1. Check known output file names
                    for fn in possible_out_names:
                        fp = os.path.join(run_dir, fn)
                        if os.path.exists(fp) and os.path.getsize(fp) > 0:
                            with open(fp, "r", encoding="utf-8", errors="replace") as fout_file:
                                out_content = fout_file.read()
                            break

                    # 2. Check any .out / .ans file in run_dir
                    if not out_content:
                        for fn in os.listdir(run_dir):
                            if fn.lower().endswith((".out", ".ans")):
                                fp = os.path.join(run_dir, fn)
                                if os.path.exists(fp) and os.path.getsize(fp) > 0:
                                    with open(fp, "r", encoding="utf-8", errors="replace") as fout_file:
                                        out_content = fout_file.read()
                                    break

                    # 3. Fallback to stdout
                    if not out_content:
                        out_content = proc.stdout

                    out_path = in_path + ".a"
                    with open(out_path, "w", encoding="utf-8") as fans:
                        fans.write(out_content)

                    test_pairs.append((in_path, out_path))
            finally:
                shutil.rmtree(run_dir, ignore_errors=True)

        test_pairs.sort(key=lambda p: int(os.path.basename(p[0])) if os.path.basename(p[0]).isdigit() else natural_sort_key(os.path.basename(p[0])))

        # 4. Standardized Test Naming (matching aplusb/test_data)
        base_inp_name = inp_file_attr if inp_file_attr else f"{problem_code.upper()}.INP"
        base_out_name = out_file_attr if out_file_attr else f"{problem_code.upper()}.OUT"

        num_tests = len(test_pairs)
        xml_test_points = [float(t.attrib.get("points", 1.0)) for t in xml_root.findall(".//testset/tests/test")]

        if points_override and float(points_override) > 0:
            target_total = round(float(points_override))
            base_pt = target_total // num_tests if num_tests else 1
            remainder = target_total % num_tests if num_tests else 0
            test_pts = [base_pt + (1 if i < remainder else 0) for i in range(num_tests)]
        elif xml_test_points and len(xml_test_points) == num_tests and all(p.is_integer() and p > 0 for p in xml_test_points):
            test_pts = [int(p) for p in xml_test_points]
        else:
            test_pts = [1 for _ in range(num_tests)]

        total_pts = float(sum(test_pts))

        data_dir = os.path.join(settings.DMOJ_PROBLEM_DATA_ROOT, problem_code)
        os.makedirs(data_dir, exist_ok=True)

        zip_out_path = os.path.join(data_dir, "tests.zip")
        init_cases = []

        with zipfile.ZipFile(zip_out_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_out:
            for idx, (in_src, out_src) in enumerate(test_pairs, 1):
                subfolder = f"test{idx:02d}"
                in_entry = f"{subfolder}/{base_inp_name}"
                out_entry = f"{subfolder}/{base_out_name}"

                zip_out.write(in_src, in_entry)
                zip_out.write(out_src, out_entry)

                pt = test_pts[idx - 1]

                init_cases.append({
                    'in': in_entry,
                    'out': out_entry,
                    'points': pt,
                })

        # Write init.yml
        init_data = {
            'archive': 'tests.zip',
            'checker': 'standard',
            'test_cases': init_cases,
        }
        init_yml_path = os.path.join(data_dir, "init.yml")
        with open(init_yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(init_data, f, sort_keys=False)

        # 5. Database Records
        default_group = ProblemGroup.objects.first()
        if not default_group:
            default_group = ProblemGroup.objects.create(name="Default", full_name="Uncategorized")

        defaults = {
            "name": problem_name,
            "description": final_description,
            "time_limit": time_limit,
            "memory_limit": memory_limit,
            "points": total_pts,
            "partial": True,
            "group": default_group,
            "is_public": is_public,
            "is_manually_managed": False,
            "date": timezone.now(),
        }
        problem, created = Problem.objects.get_or_create(code=problem_code, defaults=defaults)
        if not created:
            for k, v in defaults.items():
                setattr(problem, k, v)
            problem.save()

        problem.allowed_languages.set(Language.objects.all())

        if author_profile:
            problem.authors.add(author_profile)

        # Save ProblemData with zipfile referencing the tests.zip archive
        problem_data, _ = ProblemData.objects.get_or_create(problem=problem)
        problem_data.zipfile.name = f"{problem_code}/tests.zip"
        problem_data.checker = 'standard'
        problem_data.save()

        # Recreate ProblemTestCase objects
        ProblemTestCase.objects.filter(dataset=problem).delete()
        for idx, c in enumerate(init_cases, 1):
            ProblemTestCase.objects.create(
                dataset=problem,
                order=idx,
                type='C',
                input_file=c['in'],
                output_file=c['out'],
                points=c['points'],
                is_pretest=False,
                checker='standard',
            )

        return problem, len(test_pairs)
