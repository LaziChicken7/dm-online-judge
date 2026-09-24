import json
import mimetypes
import os
from itertools import chain
from typing import List
from zipfile import BadZipfile, ZipFile

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.forms import BaseModelFormSet, HiddenInput, ModelForm, NumberInput, Select, formset_factory
from django.http import JsonResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.generic import DetailView

from judge.highlight_code import highlight_code
from judge.models import Problem, ProblemData, ProblemTestCase, Submission, problem_data_storage
from judge.utils.problem_data import ProblemDataCompiler, detect_test_pairs
from judge.utils.unicode import utf8text
from judge.utils.views import TitleMixin, add_file_response
from judge.views.problem import ProblemMixin

mimetypes.init()
mimetypes.add_type('application/x-yaml', '.yml')


def checker_args_cleaner(self):
    data = self.cleaned_data['checker_args']
    if not data or data.isspace():
        return ''
    try:
        if not isinstance(json.loads(data), dict):
            raise ValidationError(_('Checker arguments must be a JSON object.'))
    except ValueError:
        raise ValidationError(_('Checker arguments is invalid JSON.'))
    return data


class ProblemDataForm(ModelForm):
    def clean_zipfile(self):
        if hasattr(self, 'zip_valid') and not self.zip_valid:
            raise ValidationError(_('Your zip file is invalid!'))

        zipfile = self.cleaned_data['zipfile']
        if zipfile and not zipfile.name.endswith('.zip'):
            raise ValidationError(_("Zip files must end in '.zip'"))

        return zipfile

    def clean_generator(self):
        generator = self.cleaned_data['generator']
        if generator and generator.name == 'init.yml':
            raise ValidationError(_('Generators must not be named init.yml.'))

        return generator

    clean_checker_args = checker_args_cleaner

    class Meta:
        model = ProblemData
        fields = ['zipfile', 'generator', 'unicode', 'nobigmath', 'output_limit', 'output_prefix',
                  'checker', 'checker_args']
        widgets = {
            'checker_args': HiddenInput,
        }


class ProblemCaseForm(ModelForm):
    clean_checker_args = checker_args_cleaner

    class Meta:
        model = ProblemTestCase
        fields = ('order', 'type', 'input_file', 'output_file', 'points', 'is_pretest', 'output_limit',
                  'output_prefix', 'checker', 'checker_args', 'generator_args', 'batch_dependencies')
        widgets = {
            'generator_args': HiddenInput,
            'batch_dependencies': HiddenInput,
            'type': Select(attrs={'style': 'width: 100%'}),
            'points': NumberInput(attrs={'style': 'width: 4em'}),
            'output_prefix': NumberInput(attrs={'style': 'width: 4.5em'}),
            'output_limit': NumberInput(attrs={'style': 'width: 6em'}),
            'checker_args': HiddenInput,
        }


class ProblemCaseFormSet(formset_factory(ProblemCaseForm, formset=BaseModelFormSet, extra=1, max_num=1,
                                         can_delete=True)):
    model = ProblemTestCase

    def __init__(self, *args, **kwargs):
        self.valid_files = kwargs.pop('valid_files', None)
        super(ProblemCaseFormSet, self).__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        form = super(ProblemCaseFormSet, self)._construct_form(i, **kwargs)
        form.valid_files = self.valid_files
        return form


class ProblemManagerMixin(LoginRequiredMixin, ProblemMixin, DetailView):
    def get_object(self, queryset=None):
        problem = super(ProblemManagerMixin, self).get_object(queryset)
        if problem.is_manually_managed:
            raise Http404()
        if self.request.user.is_superuser or problem.is_editable_by(self.request.user):
            return problem
        raise Http404()


class ProblemSubmissionDiff(TitleMixin, ProblemMixin, DetailView):
    template_name = 'problem/submission-diff.html'

    def get_title(self):
        return _('Comparing submissions for {0}').format(self.object.name)

    def get_content_title(self):
        return mark_safe(escape(_('Comparing submissions for {0}')).format(
            format_html('<a href="{1}">{0}</a>', self.object.name, reverse('problem_detail', args=[self.object.code])),
        ))

    def get_object(self, queryset=None):
        problem = super(ProblemSubmissionDiff, self).get_object(queryset)
        if self.request.user.is_superuser or problem.is_editable_by(self.request.user):
            return problem
        raise Http404()

    def get_context_data(self, **kwargs):
        context = super(ProblemSubmissionDiff, self).get_context_data(**kwargs)
        try:
            ids = self.request.GET.getlist('id')
            subs = Submission.objects.filter(id__in=ids)
        except ValueError:
            raise Http404
        if not subs:
            raise Http404

        context['submissions'] = subs

        # If we have associated data we can do better than just guess
        data = ProblemTestCase.objects.filter(dataset=self.object, type='C')
        if data:
            num_cases = data.count()
        else:
            num_cases = subs.first().test_cases.count()
        context['num_cases'] = num_cases
        return context


class ProblemDataView(TitleMixin, ProblemManagerMixin):
    template_name = 'problem/data.html'

    def get_title(self):
        return _('Editing data for {0}').format(self.object.name)

    def get_content_title(self):
        return mark_safe(escape(_('Editing data for %s')) % (
            format_html('<a href="{1}">{0}</a>', self.object.name,
                        reverse('problem_detail', args=[self.object.code]))))

    def get_data_form(self, post=False):
        return ProblemDataForm(data=self.request.POST if post else None, prefix='problem-data',
                               files=self.request.FILES if post else None,
                               instance=ProblemData.objects.get_or_create(problem=self.object)[0])

    def get_case_formset(self, files, post=False):
        return ProblemCaseFormSet(data=self.request.POST if post else None, prefix='cases', valid_files=files,
                                  queryset=ProblemTestCase.objects.filter(dataset_id=self.object.pk).order_by('order'))

    def get_valid_files(self, data, post=False) -> List[str]:
        try:
            if post and 'problem-data-zipfile-clear' in self.request.POST:
                return []
            elif post and 'problem-data-zipfile' in self.request.FILES:
                return ZipFile(self.request.FILES['problem-data-zipfile']).namelist()
            elif data.zipfile:
                return ZipFile(data.zipfile.path).namelist()
        except BadZipfile:
            raise
        return []

    def get_context_data(self, **kwargs):
        context = super(ProblemDataView, self).get_context_data(**kwargs)
        valid_files = []
        if 'data_form' not in context:
            context['data_form'] = self.get_data_form()
            try:
                valid_files = self.get_valid_files(context['data_form'].instance)
            except BadZipfile:
                pass
        context['valid_files'] = set(valid_files)
        context['valid_files_json'] = mark_safe(json.dumps(valid_files))

        context['cases_formset'] = self.get_case_formset(valid_files)
        context['all_case_forms'] = chain(context['cases_formset'], [context['cases_formset'].empty_form])
        try:
            context['ac_submissions'] = (
                self.object.submission_set.filter(result='AC')
                .select_related('user', 'language')
                .order_by('-id')[:30]
            )
        except Exception:
            context['ac_submissions'] = []
        return context

    def post(self, request, *args, **kwargs):
        self.object = problem = self.get_object()
        data_form = self.get_data_form(post=True)

        if 'problem-data-zipfile-clear' in request.POST:
            if data_form.is_valid():
                data = data_form.save()
                problem.cases.all().delete()
                ProblemDataCompiler.generate(problem, data, problem.cases.all(), [])
                return HttpResponseRedirect(request.get_full_path())

        try:
            valid_files = self.get_valid_files(data_form.instance, post=True)
            data_form.zip_valid = True
        except BadZipfile:
            valid_files = []
            data_form.zip_valid = False

        cases_formset = self.get_case_formset(valid_files, post=True)
        if data_form.is_valid() and cases_formset.is_valid():
            data = data_form.save()
            cases_saved = cases_formset.save(commit=False)
            for case in cases_saved:
                case.dataset_id = problem.id
                case.save()
            for case in cases_formset.deleted_objects:
                case.delete()

            has_real_cases = problem.cases.exclude(input_file='', output_file='').exists()
            if not has_real_cases and valid_files:
                pairs = detect_test_pairs(valid_files)
                if pairs:
                    problem.cases.all().delete()
                    for order, (inp, out) in enumerate(pairs, 1):
                        ProblemTestCase.objects.create(
                            dataset=problem,
                            order=order,
                            type='C',
                            input_file=inp,
                            output_file=out,
                            points=1,
                            is_pretest=False,
                            generator_args='',
                            checker='',
                            checker_args='',
                            batch_dependencies='',
                        )

            ProblemDataCompiler.generate(problem, data, problem.cases.order_by('order'), valid_files)
            return HttpResponseRedirect(request.get_full_path())
        return self.render_to_response(self.get_context_data(data_form=data_form, cases_formset=cases_formset,
                                                             valid_files=valid_files))

    put = post


@login_required
def problem_data_file(request, problem, path):
    object = get_object_or_404(Problem, code=problem)
    if not object.is_editable_by(request.user):
        raise Http404()

    problem_dir = problem_data_storage.path(problem)
    if os.path.commonpath((problem_data_storage.path(os.path.join(problem, path)), problem_dir)) != problem_dir:
        raise Http404()

    response = HttpResponse()

    if hasattr(settings, 'DMOJ_PROBLEM_DATA_INTERNAL'):
        url_path = '%s/%s/%s' % (settings.DMOJ_PROBLEM_DATA_INTERNAL, problem, path)
    else:
        url_path = None

    try:
        add_file_response(request, response, url_path, os.path.join(problem, path), problem_data_storage)
    except IOError:
        raise Http404()

    response['Content-Type'] = 'application/octet-stream'
    return response


@login_required
def problem_init_view(request, problem):
    problem = get_object_or_404(Problem, code=problem)
    if not problem.is_editable_by(request.user):
        raise Http404()

    try:
        with problem_data_storage.open(os.path.join(problem.code, 'init.yml'), 'rb') as f:
            data = utf8text(f.read()).rstrip('\n')
    except IOError:
        raise Http404()

    return render(request, 'problem/yaml.html', {
        'raw_source': data, 'highlighted_source': highlight_code(data, 'yaml'),
        'title': _('Generated init.yml for %s') % problem.name,
        'content_title': mark_safe(escape(_('Generated init.yml for %s')) % (
            format_html('<a href="{1}">{0}</a>', problem.name,
                        reverse('problem_detail', args=[problem.code])))),
    })


def natural_sort_key(s):
    import re
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', str(s))]

@login_required
def problem_submission_source_view(request, problem, sub_id):
    problem_obj = get_object_or_404(Problem, code=problem)
    if not (request.user.is_superuser or problem_obj.is_editable_by(request.user)):
        return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)
    try:
        sub = Submission.objects.get(id=sub_id, problem=problem_obj)
        return JsonResponse({
            'success': True,
            'source': sub.source.source,
            'language': sub.language.key if sub.language else 'CPP17',
            'user': sub.user.username,
        })
    except Submission.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Submission not found'}, status=404)

@login_required
def problem_polygon_generate_view(request, problem):
    import re
    import shutil
    import subprocess
    import tempfile
    import zipfile
    
    problem_obj = get_object_or_404(Problem, code=problem)
    if not (request.user.is_superuser or problem_obj.is_editable_by(request.user)):
        return JsonResponse({'success': False, 'error': 'Bạn không có quyền chỉnh sửa bài tập này.'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Yêu cầu phương thức POST'}, status=405)

    source_code = request.POST.get('source_code', '').strip()
    language = request.POST.get('language', 'CPP17').upper().strip()
    sub_id = request.POST.get('submission_id')
    try:
        time_limit = float(request.POST.get('time_limit', problem_obj.time_limit or 2.0))
    except (ValueError, TypeError):
        time_limit = 2.0

    if sub_id:
        try:
            sub = Submission.objects.get(id=int(sub_id), problem=problem_obj)
            source_code = sub.source.source
            if sub.language:
                language = sub.language.key.upper()
        except Exception:
            pass
    elif 'source_file' in request.FILES:
        try:
            source_code = request.FILES['source_file'].read().decode('utf-8', errors='replace')
        except Exception as e:
            return JsonResponse({'success': False, 'error': f'Không thể đọc file mã nguồn: {str(e)}'})

    if not source_code:
        return JsonResponse({'success': False, 'error': 'Vui lòng cung cấp mã nguồn giải mẫu (Model Solution)!'})

    with tempfile.TemporaryDirectory() as tmp_dir:
        inputs_dir = os.path.join(tmp_dir, 'inputs')
        outputs_dir = os.path.join(tmp_dir, 'outputs')
        os.makedirs(inputs_dir, exist_ok=True)
        os.makedirs(outputs_dir, exist_ok=True)

        # 1. Thu thập file input
        input_file_list = request.FILES.getlist('input_files') or request.FILES.getlist('files')
        zip_upload = request.FILES.get('zip_file')

        if zip_upload:
            try:
                with ZipFile(zip_upload, 'r') as zf:
                    zf.extractall(inputs_dir)
            except Exception as e:
                return JsonResponse({'success': False, 'error': f'Lỗi giải nén file ZIP: {str(e)}'})
        elif input_file_list:
            for up_file in input_file_list:
                fname = os.path.basename(up_file.name)
                with open(os.path.join(inputs_dir, fname), 'wb') as f:
                    for chunk in up_file.chunks():
                        f.write(chunk)
        else:
            existing_zip_path = None
            p_data = ProblemData.objects.filter(problem=problem_obj).first()
            if p_data and bool(p_data.zipfile):
                try:
                    if os.path.exists(p_data.zipfile.path):
                        existing_zip_path = p_data.zipfile.path
                except Exception:
                    pass

            if not existing_zip_path:
                prob_dir = problem_data_storage.path(problem_obj.code)
                if os.path.exists(prob_dir):
                    for f in os.listdir(prob_dir):
                        if f.lower().endswith('.zip'):
                            existing_zip_path = os.path.join(prob_dir, f)
                            break

            if existing_zip_path and os.path.exists(existing_zip_path):
                try:
                    with ZipFile(existing_zip_path, 'r') as zf:
                        zf.extractall(inputs_dir)
                except Exception as e:
                    return JsonResponse({'success': False, 'error': f'Lỗi đọc ZIP hiện tại: {str(e)}'})
            else:
                return JsonResponse({'success': False, 'error': 'Chưa chọn file input nào (.inp, .in) và bài tập chưa có file zip!'})

        # Quét danh sách file input
        candidates = []
        for root, _, files in os.walk(inputs_dir):
            for f in files:
                if f.startswith('.') or f.endswith(('.out', '.OUT', '.ans', '.ANS', '.yml', '.yaml', '.zip', '.exe', '.pyc')):
                    continue
                rel = os.path.relpath(os.path.join(root, f), inputs_dir).replace('\\', '/')
                if re.search(r'\.(inp|in|txt)$', f, re.I) or re.search(r'(^|[/_.-])input', f, re.I) or 'test' in rel.lower():
                    candidates.append(rel)

        if not candidates:
            for root, _, files in os.walk(inputs_dir):
                for f in files:
                    if f.startswith('.') or f.endswith(('.out', '.OUT', '.ans', '.ANS', '.yml', '.yaml', '.zip', '.exe', '.pyc')):
                        continue
                    rel = os.path.relpath(os.path.join(root, f), inputs_dir).replace('\\', '/')
                    candidates.append(rel)

        if not candidates:
            return JsonResponse({'success': False, 'error': 'Không tìm thấy file input nào trong dữ liệu tải lên!'})

        candidates.sort(key=natural_sort_key)

        # 2. Biên dịch lời giải mẫu
        exec_cmd = None
        if any(k in language for k in ('CPP', 'C++', 'C11', 'C')) and language != 'PY3':
            is_c = (language in ('C', 'C11'))
            src_name = 'solution.c' if is_c else 'solution.cpp'
            bin_name = 'solution'
            src_path = os.path.join(tmp_dir, src_name)
            bin_path = os.path.join(tmp_dir, bin_name)
            with open(src_path, 'w', encoding='utf-8') as f:
                f.write(source_code)

            compiler = 'gcc' if is_c else 'g++'
            compile_cmd = [compiler, '-O3']
            if language == 'CPP20':
                compile_cmd.append('-std=c++20')
            elif language == 'CPP17':
                compile_cmd.append('-std=c++17')
            elif language == 'CPP11':
                compile_cmd.append('-std=c++11')
            elif language == 'C11':
                compile_cmd.append('-std=c11')
            else:
                compile_cmd.append('-std=c++14')
            compile_cmd += [src_name, '-o', bin_name]

            comp_res = subprocess.run(compile_cmd, cwd=tmp_dir, capture_output=True, text=True, timeout=30)
            if comp_res.returncode != 0:
                return JsonResponse({'success': False, 'error': 'Lỗi biên dịch C/C++ (Compilation Error)!', 'details': comp_res.stderr})
            exec_cmd = [bin_path]
        elif 'PY' in language:
            src_path = os.path.join(tmp_dir, 'solution.py')
            with open(src_path, 'w', encoding='utf-8') as f:
                f.write(source_code)
            check_res = subprocess.run(['python3', '-m', 'py_compile', 'solution.py'], cwd=tmp_dir, capture_output=True, text=True)
            if check_res.returncode != 0:
                return JsonResponse({'success': False, 'error': 'Lỗi cú pháp Python (Syntax Error)!', 'details': check_res.stderr})
            exec_cmd = ['python3', src_path]
        elif 'PAS' in language:
            src_path = os.path.join(tmp_dir, 'solution.pas')
            with open(src_path, 'w', encoding='utf-8') as f:
                f.write(source_code)
            comp_res = subprocess.run(['fpc', '-O2', 'solution.pas'], cwd=tmp_dir, capture_output=True, text=True, timeout=30)
            if comp_res.returncode != 0:
                return JsonResponse({'success': False, 'error': 'Lỗi biên dịch Pascal!', 'details': comp_res.stderr})
            exec_cmd = [os.path.join(tmp_dir, 'solution')]
        else:
            src_path = os.path.join(tmp_dir, 'solution.cpp')
            bin_path = os.path.join(tmp_dir, 'solution')
            with open(src_path, 'w', encoding='utf-8') as f:
                f.write(source_code)
            comp_res = subprocess.run(['g++', '-O3', '-std=c++17', 'solution.cpp', '-o', 'solution'], cwd=tmp_dir, capture_output=True, text=True, timeout=30)
            if comp_res.returncode != 0:
                return JsonResponse({'success': False, 'error': 'Lỗi biên dịch C++!', 'details': comp_res.stderr})
            exec_cmd = [bin_path]

        # 3. Chạy lời giải trên từng file input
        pairs = []
        p_code = problem_obj.code
        run_workspace = os.path.join(tmp_dir, 'run_case')

        for inp_rel in candidates:
            if re.search(r'\.inp$', inp_rel, re.I):
                out_rel = re.sub(r'\.inp$', '.out', inp_rel, flags=re.I)
            elif re.search(r'\.in$', inp_rel, re.I):
                out_rel = re.sub(r'\.in$', '.out', inp_rel, flags=re.I)
            elif re.search(r'\.txt$', inp_rel, re.I):
                out_rel = re.sub(r'\.txt$', '.out', inp_rel, flags=re.I)
            else:
                out_rel = inp_rel + '.out'

            shutil.rmtree(run_workspace, ignore_errors=True)
            os.makedirs(run_workspace, exist_ok=True)

            inp_abs = os.path.join(inputs_dir, inp_rel)
            with open(inp_abs, 'rb') as f:
                inp_bytes = f.read()

            base_inp = os.path.basename(inp_rel)
            alias_names = {
                base_inp,
                base_inp.lower(),
                base_inp.upper(),
                f'{p_code}.inp',
                f'{p_code.lower()}.inp',
                f'{p_code.upper()}.INP',
                'input.txt',
                'INPUT.TXT',
            }
            for alias in alias_names:
                try:
                    with open(os.path.join(run_workspace, alias), 'wb') as f:
                        f.write(inp_bytes)
                except Exception:
                    pass

            try:
                proc = subprocess.run(
                    exec_cmd,
                    input=inp_bytes,
                    cwd=run_workspace,
                    capture_output=True,
                    timeout=time_limit + 1.0
                )
            except subprocess.TimeoutExpired:
                return JsonResponse({'success': False, 'error': f'Test {inp_rel} chạy quá thời gian (TLE > {time_limit}s)!'})

            if proc.returncode != 0:
                err_msg = proc.stderr.decode('utf-8', errors='replace')
                return JsonResponse({'success': False, 'error': f'Test {inp_rel} bị lỗi thực thi (RTE - Mã lỗi {proc.returncode})!', 'details': err_msg})

            base_out = os.path.basename(out_rel)
            out_candidates = [
                base_out,
                base_out.lower(),
                base_out.upper(),
                f'{p_code}.out',
                f'{p_code.lower()}.out',
                f'{p_code.upper()}.OUT',
                'output.txt',
                'OUTPUT.TXT',
            ]
            captured_output = None
            for out_name in out_candidates:
                out_file_path = os.path.join(run_workspace, out_name)
                if os.path.exists(out_file_path) and os.path.getsize(out_file_path) > 0:
                    with open(out_file_path, 'rb') as f:
                        captured_output = f.read()
                    break

            if captured_output is None:
                captured_output = proc.stdout

            out_abs = os.path.join(outputs_dir, out_rel)
            os.makedirs(os.path.dirname(out_abs), exist_ok=True)
            with open(out_abs, 'wb') as f:
                f.write(captured_output)

            pairs.append((inp_rel, out_rel))

        # 4. Đóng gói file ZIP hoàn chỉnh (chứa cả .inp và .out)
        prob_storage_dir = os.path.join(settings.DMOJ_PROBLEM_DATA_ROOT, problem_obj.code)
        os.makedirs(prob_storage_dir, exist_ok=True)
        zip_filename = f"{problem_obj.code.upper()}_Tests.zip"
        zip_dest = os.path.join(prob_storage_dir, zip_filename)

        with ZipFile(zip_dest, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for inp_rel, out_rel in pairs:
                zf.write(os.path.join(inputs_dir, inp_rel), inp_rel)
                zf.write(os.path.join(outputs_dir, out_rel), out_rel)

        # 5. Cập nhật cơ sở dữ liệu ProblemData và ProblemTestCase
        p_data, _ = ProblemData.objects.get_or_create(problem=problem_obj)
        p_data.zipfile = f"{problem_obj.code}/{zip_filename}"
        p_data.feedback = ''
        p_data.save()

        problem_obj.cases.all().delete()
        for order, (inp_rel, out_rel) in enumerate(pairs, 1):
            ProblemTestCase.objects.create(
                dataset=problem_obj,
                order=order,
                type='C',
                input_file=inp_rel,
                output_file=out_rel,
                points=1,
                is_pretest=False,
                generator_args='',
                checker='',
                checker_args='',
                batch_dependencies='',
            )

        with ZipFile(zip_dest, 'r') as zf:
            valid_files = zf.namelist()
        ProblemDataCompiler.generate(problem_obj, p_data, problem_obj.cases.order_by('order'), valid_files)

        return JsonResponse({
            'success': True,
            'message': f'Đã sinh thành công {len(pairs)} test cases (.inp -> .out) và lưu dữ liệu bài tập!',
            'count': len(pairs),
            'pairs': pairs,
            'zip_name': zip_filename,
        })
