from __future__ import annotations

import json
from pathlib import Path

import pytest

from eurus.models import Message, Usage
from eurus.office.models.task import OfficeTask
from eurus.office.qa.scoring import AcceptanceThresholds
from eurus.office.qa.structural import validate_structure
from eurus.office.renderers.base import detect_capabilities
from eurus.office.specialist.budget import BudgetLimits
from eurus.office.specialist.manager import OfficeSpecialist
from eurus.office.storage import JobWorkspace, brief_from_payload, create_job_id
from eurus.providers.base import ChatResponse
from eurus.tools.base import ToolContext

docx = pytest.importorskip("docx")
openpyxl = pytest.importorskip("openpyxl")
pptx = pytest.importorskip("pptx")

CAPABILITIES = detect_capabilities()

CLIENT_ANALYSIS = {
    "audience": "pelajar dan guru sejarah",
    "tone": "edukatif formal",
    "style": "editorial",
    "requirements": [
        "cover profesional",
        "struktur bab sejarah",
        "tabel dinasti",
        "kronologi",
        "referensi",
    ],
    "acceptance_criteria": ["heading konsisten", "tabel nyata", "halaman bernomor"],
    "research_needed": False,
    "research_questions": [],
    "language": "id",
}

RESEARCH_MATARAM = {
    "facts": [
        {
            "statement": "Sanjaya mendirikan dinasti Sanjaya pada 732 M",
            "kind": "verified",
            "source": "Prasasti Canggal",
        },
        {
            "statement": "Pusat kerajaan di Jawa Tengah, likely Kedu Plain",
            "kind": "verified",
            "source": "Sejarah umum",
        },
        {
            "statement": "Candi Prambanan dibangun masa Rakai Pikatan",
            "kind": "verified",
            "source": "Prasasti Shivagrha",
        },
        {
            "statement": "Pusat pindah ke Jawa Timur sekitar 929 M",
            "kind": "verified",
            "source": "Sejarah umum",
        },
    ],
    "sources": ("Prasasti Canggal", "Prasasti Shivagrha"),
    "outline": [
        "Pendahuluan",
        "Dinasti Sanjaya",
        "Dinasti Syailendra",
        "Prambanan",
        "Pindah ke Jawa Timur",
        "Warisan",
    ],
    "gaps": ["Perbatasan kekuasaan pasti tidak terdokumentasi"],
}

DESIGN_MATARAM = {
    "direction": "historical",
    "document_style": "editorial",
    "guidance": ["palet kertas kuno", "serif"],
    "palette_notes": "cokelat hangat",
    "typography_notes": "Georgia",
    "layout_plan": ["cover", "toc", "bab", "tabel", "kronologi", "referensi"],
}

CLIENT_ANALYSIS_DECK = {
    "audience": "siswa SMP",
    "tone": "edukatif",
    "style": "educational",
    "requirements": [
        "cover",
        "agenda",
        "pembatas bab",
        "timeline",
        "perbandingan",
        "diagram proses",
        "kesimpulan",
        "referensi",
    ],
    "acceptance_criteria": ["variasi layout", "bukan teks polos"],
    "research_needed": False,
    "research_questions": [],
    "language": "id",
}

RESEARCH_GENERIC = {
    "facts": [
        {
            "statement": "Fotosintesis menghasilkan oksigen",
            "kind": "verified",
            "source": "buku biologi",
        },
        {
            "statement": "Air tersusun dari hidrogen dan oksigen",
            "kind": "verified",
            "source": "kimia dasar",
        },
    ],
    "sources": ["buku pelajaran"],
    "outline": ["Pengantar", "Proses", "Perbandingan", "Siklus", "Kesimpulan"],
    "gaps": [],
}

DESIGN_DECK = {
    "direction": "educational",
    "document_style": "educational",
    "guidance": ["hijau segar", "ilustratif"],
    "palette_notes": "hijau",
    "typography_notes": "sans",
    "layout_plan": [
        "cover",
        "agenda",
        "divider",
        "content",
        "timeline",
        "process",
        "metrics",
        "chart",
        "comparison",
        "quote",
        "case_study",
        "conclusion",
        "references",
    ],
}

CLIENT_ANALYSIS_FINANCE = {
    "audience": "analyst keuangan",
    "tone": "formal",
    "style": "business",
    "requirements": [
        "data transaksi",
        "rumus",
        "analisis",
        "dashboard",
        "grafik",
        "format bersyarat",
    ],
    "acceptance_criteria": ["formula hidup", "chart native", "conditional formatting"],
    "research_needed": False,
    "research_questions": [],
    "language": "id",
}

RESEARCH_FINANCE = {
    "facts": [
        {
            "statement": "Data berasal dari berkas sumber",
            "kind": "verified",
            "source": "input pengguna",
        }
    ],
    "sources": ["input"],
    "outline": ["Overview", "Transactions", "Analysis", "Dashboard"],
    "gaps": [],
}

DESIGN_FINANCE = {
    "direction": "corporate",
    "document_style": "business",
    "guidance": ["navy", "bersih"],
    "palette_notes": "navy",
    "typography_notes": "sans",
    "layout_plan": ["overview", "data", "dashboard"],
}

FINANCE_ROWS = [
    ("2026-01-05", "Penjualan A", 120),
    ("2026-01-19", "Penjualan B", 85),
    ("2026-02-03", "Penjualan A", 132),
    ("2026-02-14", "Biaya operasional", -40),
    ("2026-03-02", "Penjualan C", 97),
    ("2026-03-21", "Penjualan A", 143),
    ("2026-04-04", "Biaya pemasaran", -55),
    ("2026-05-01", "Penjualan B", 118),
    ("2026-05-22", "Penjualan C", 155),
    ("2026-06-10", "Biaya operasional", -46),
    ("2026-06-24", "Penjualan A", 161),
    ("2026-06-30", "Penyesuaian", -12),
]

BENCHMARK_DOCX_SPEC = {
    "kind": "document",
    "metadata": {
        "title": "Kerajaan Mataram Kuno",
        "subtitle": "Laporan sejarah untuk pembelajaran",
        "author": "Eurus",
        "date_text": "2026",
        "subject": "Sejarah Indonesia",
        "keywords": ["mataram", "sanjaya", "syailendra"],
    },
    "style": "editorial",
    "include_toc": True,
    "header_footer": {
        "header_text": "Kerajaan Mataram Kuno",
        "footer_text": "Eurus Office Department",
        "page_numbers": True,
    },
    "blocks": [
        {"kind": "heading", "level": 1, "text": "Pendahuluan"},
        {
            "kind": "paragraph",
            "text": "Mataram Kuno adalah kerajaan bercorak Hindu-Buddha yang berpusat di "
            "Jawa Tengah sekitar abad ke-8 hingga abad ke-10 Masehi. "
            "**Prasasti Canggal** tahun 732 M menjadi salah satu bukti tertulis tertua.",
        },
        {
            "kind": "callout",
            "title": "Prasasti Canggal",
            "body": "Dibuat tahun 732 M dan menyinggung raja Sanjaya.",
            "variant": "info",
        },
        {"kind": "heading", "level": 1, "text": "Dinasti Sanjaya"},
        {
            "kind": "paragraph",
            "text": "Dinasti Sanjaya bercorak Hindu Shiwa. Raja-raja pentingnya "
            "tercatat pada tabel berikut.",
        },
        {
            "kind": "table",
            "headers": ["Raja", "Perkiraan Masa", "Catatan"],
            "rows": [
                ["Sanjaya", "732-760", "Prasasti Canggal"],
                ["Rakai Panangkaran", "760-780", "Pembangunan Kalasan"],
                ["Rakai Pikatan", "830-850an", "Pernikahan dengan Pramodawardhani"],
            ],
            "caption": "Tabel 1. Raja-raja Dinasti Sanjaya",
        },
        {"kind": "heading", "level": 2, "text": "Hubungan dengan Syailendra"},
        {
            "kind": "paragraph",
            "text": "Dinasti Syailendra bercorak Buddha Mahayana dan membangun "
            "Borobudur. Hubungan kedua dinasti diperkuat melalui pernikahan politik.",
        },
        {"kind": "heading", "level": 1, "text": "Kronologi Penting"},
        {
            "kind": "timeline",
            "items": [
                {
                    "label": "732",
                    "title": "Sanjaya berkuasa",
                    "description": "Prasasti Canggal didirikan",
                },
                {"label": "778", "title": "Candi Kalasan", "description": "Kerja sama dua dinasti"},
                {"label": "856", "title": "Pertempuran", "description": "Penyatuan kekuasaan"},
                {
                    "label": "929",
                    "title": "Pindah ke Jawa Timur",
                    "description": "Mpu Sindok mendirikan Medang",
                },
            ],
        },
        {"kind": "heading", "level": 1, "text": "Warisan"},
        {
            "kind": "paragraph",
            "text": "Candi Prambanan yang dibangun masa Rakai Pikatan menjadi warisan "
            "terbesar era akhir Mataram Kuno di Jawa Tengah.",
        },
        {
            "kind": "quote",
            "text": "Prasasti adalah jendela utama menuju sejarah Jawa kuno.",
            "attribution": "Epigrafi Indonesia",
        },
        {"kind": "page_break"},
        {"kind": "heading", "level": 1, "text": "Penutup"},
        {
            "kind": "paragraph",
            "text": "Laporan ini merangkum periode Mataram Kuno untuk keperluan "
            "pembelajaran sejarah.",
        },
        {
            "kind": "bibliography",
            "entries": [
                "Van Naerssen, F. H. 1947. Canggal Inscription.",
                "Coedes, G. 1968. The Indianized States of Southeast Asia.",
                "Zoetmulder, P. J. 1982. Kalangwan.",
            ],
        },
    ],
}

BENCHMARK_PPTX_SPEC = {
    "kind": "presentation",
    "title": "Siklus Air",
    "subtitle": "Deck sains untuk siswa",
    "author": "Eurus",
    "direction": "educational",
    "footer_text": "Kelas Sains",
    "slides": [
        {
            "layout": "cover",
            "title": "Siklus Air",
            "subtitle": "Perjalanan tanpa akhir",
            "kicker": "IPA Kelas 7",
        },
        {
            "layout": "agenda",
            "title": "Agenda",
            "bullets": ["Pengantar", "Proses", "Perbandingan", "Siklus di kehidupan", "Kesimpulan"],
        },
        {"layout": "section_divider", "title": "Bagaimana air berpindah?", "kicker": "01"},
        {
            "layout": "content",
            "title": "Tiga proses utama",
            "bullets": ["Evaporasi", "Kondensasi", "Presipitasi"],
            "callout": "Matahari adalah penggeraknya.",
        },
        {
            "layout": "timeline",
            "title": "Perjalanan setetes air",
            "timeline": [
                {"label": "1", "title": "Menguap dari laut", "description": "Evaporasi"},
                {"label": "2", "title": "Membentuk awan", "description": "Kondensasi"},
                {"label": "3", "title": "Turun sebagai hujan", "description": "Presipitasi"},
                {"label": "4", "title": "Kembali ke laut", "description": "Aliran sungai"},
            ],
        },
        {
            "layout": "process",
            "title": "Stasiun pemantauan hujan",
            "process": [
                {"title": "Ukur curah hujan", "description": "Alat penakar"},
                {"title": "Catat harian", "description": "Logbook"},
                {"title": "Grafik bulanan", "description": "Visualisasi"},
            ],
        },
        {
            "layout": "metrics",
            "title": "Angka menarik",
            "metrics": [
                {"label": "Air di laut", "value": "97%", "detail": "dari total air Bumi"},
                {"label": "Air tawar", "value": "3%", "detail": "sebagian beku"},
                {"label": "Siklus", "value": "Berulang", "detail": "tanpa akhir"},
            ],
        },
        {
            "layout": "chart",
            "title": "Curah hujan contoh",
            "chart": {
                "kind": "column",
                "title": "mm per bulan",
                "categories": ["Jan", "Feb", "Mar", "Apr"],
                "series": [
                    {"name": "Stasiun A", "values": [210, 190, 150, 90]},
                    {"name": "Stasiun B", "values": [180, 175, 160, 95]},
                ],
            },
            "bullets": ["Musim hujan terlihat jelas"],
        },
        {
            "layout": "comparison",
            "title": "Evaporasi vs Kondensasi",
            "comparison": [
                {"title": "Evaporasi", "points": ["Cair ke gas", "Butuh panas"]},
                {"title": "Kondensasi", "points": ["Gas ke cair", "Melepas panas"]},
            ],
        },
        {
            "layout": "quote",
            "title": "Renungan",
            "quote": "Air yang kamu minum hari ini mungkin pernah menjadi hujan ribuan tahun lalu.",
            "attribution": "Guru Sains",
        },
        {
            "layout": "case_study",
            "title": "Studi kasus waduk",
            "bullets": ["Menampung hujan musiman", "Mengairi sawah"],
            "metrics": [{"label": "Kapasitas", "value": "12 jt m3", "detail": "estimasi"}],
        },
        {
            "layout": "conclusion",
            "title": "Kesimpulan",
            "bullets": ["Siklus air menjaga ketersediaan", "Manusia harus menjaga kualitasnya"],
            "callout": "Tugas: amati siklus di rumahmu.",
        },
        {
            "layout": "references",
            "title": "Referensi",
            "references": ["Buku IPA Kelas 7 (2024).", "BMKG. Materi siklus hidrologi."],
        },
    ],
}

BENCHMARK_XLSX_SPEC = {
    "kind": "workbook",
    "title": "Analisis Keuangan H1 2026",
    "subject": "Keuangan",
    "recalc_on_load": True,
    "named_ranges": [{"name": "AmountColumn", "reference": "Transactions!$C$2:$C$13"}],
    "sheets": [
        {
            "name": "Overview",
            "role": "overview",
            "tables": [
                {
                    "headers": ["Ringkasan", "Nilai"],
                    "rows": [
                        ["Periode", "H1 2026"],
                        ["Jumlah transaksi", 12],
                        ["Sumber", "input pengguna"],
                    ],
                    "start_cell": "A1",
                }
            ],
            "notes": ["Workbook ini dihasilkan otomatis oleh Eurus Office Department."],
        },
        {
            "name": "Transactions",
            "role": "data",
            "tables": [
                {
                    "headers": ["Tanggal", "Keterangan", "Jumlah"],
                    "rows": [[row[0], row[1], row[2]] for row in FINANCE_ROWS],
                    "start_cell": "A1",
                    "allow_formulas": True,
                }
            ],
            "formula_columns": [
                {
                    "header": "Saldo Berjalan",
                    "pattern": "=SUM($C$2:C{row})",
                    "number_format": "#,##0",
                }
            ],
            "column_formats": {"C": "#,##0;[Red]-#,##0"},
            "freeze_panes": "A2",
            "auto_filter": True,
            "conditional": [{"kind": "color_scale", "range": "C2:C13"}],
        },
        {
            "name": "Analysis",
            "role": "calculations",
            "tables": [
                {
                    "headers": ["Metrik", "Nilai"],
                    "rows": [
                        ["Total pemasukan", '=SUMIF(Transactions!C2:C13,">0")'],
                        ["Total pengeluaran", '=SUMIF(Transactions!C2:C13,"<0")'],
                        ["Arus kas bersih", "=SUM(Transactions!C2:C13)"],
                        ["Rata-rata bulanan", "=AVERAGE(Transactions!C2:C13)"],
                    ],
                    "start_cell": "A1",
                    "allow_formulas": True,
                }
            ],
        },
        {
            "name": "Dashboard",
            "role": "dashboard",
            "kpis": [
                {
                    "label": "Arus kas bersih",
                    "formula": "=SUM(Transactions!C2:C13)",
                    "number_format": "#,##0",
                    "detail": "H1 2026",
                },
                {
                    "label": "Transaksi terbesar",
                    "formula": "=MAX(Transactions!C2:C13)",
                    "number_format": "#,##0",
                    "detail": "puncak",
                },
                {
                    "label": "Jumlah baris",
                    "formula": "=COUNTA(Transactions!A2:A13)",
                    "number_format": "0",
                    "detail": "data",
                },
            ],
            "charts": [
                {
                    "kind": "column",
                    "title": "Arus kas per transaksi",
                    "categories_ref": "Transactions!B2:B13",
                    "series": [{"name": "Jumlah", "ref": "Transactions!C2:C13"}],
                    "anchor": "F3",
                }
            ],
            "conditional": [{"kind": "data_bar", "range": "Transactions!C2:C13"}],
        },
    ],
}


class BenchRouter:
    def __init__(self, mapping) -> None:
        self.mapping = mapping
        self.calls = 0

    async def chat(self, messages, *, tools=(), temperature=None, max_tokens=None):
        self.calls += 1
        system = messages[0].content
        user = messages[1].content
        if "evaluating the finished document" in user:
            payload = self.mapping["accept"]
        elif "Critic Agent" in system:
            payload = self.mapping["critic"]
        elif "Client Agent" in system:
            payload = self.mapping["analysis"]
        elif "Research Agent" in system:
            payload = self.mapping["research"]
        elif "Design Agent" in system:
            payload = self.mapping["design"]
        elif "Production Agent" in system:
            payload = self.mapping["spec"]
        else:
            raise AssertionError("unexpected role prompt")
        return ChatResponse(
            message=Message.assistant(json.dumps(payload)), usage=Usage(), finish_reason="stop"
        )


async def run_benchmark(tmp_path: Path, fmt: str, output_path: str, mapping) -> object:
    router = BenchRouter(mapping)
    context = ToolContext(workspace=tmp_path)
    brief = brief_from_payload(
        {
            "objective": mapping["objective"],
            "format": fmt,
            "language": "id",
            "output_path": output_path,
        }
    )
    task = OfficeTask(task_id="bench", job_id=create_job_id(), brief=brief)
    workspace = JobWorkspace.create(task.job_id)
    specialist = OfficeSpecialist(
        router,
        context,
        task,
        workspace=workspace,
        limits=BudgetLimits(max_total_model_calls=80, max_revision_cycles=2, deadline_seconds=1200),
        thresholds=AcceptanceThresholds(),
    )
    return await specialist.run()


@pytest.mark.asyncio
async def test_benchmark_a_historical_docx_report(tmp_path: Path) -> None:
    result = await run_benchmark(
        tmp_path,
        "docx",
        "benchmark-a/mataram.docx",
        {
            "objective": "Buat laporan sejarah Kerajaan Mataram Kuno untuk pembelajaran.",
            "spec": BENCHMARK_DOCX_SPEC,
            "analysis": CLIENT_ANALYSIS,
            "research": RESEARCH_MATARAM,
            "design": DESIGN_MATARAM,
            "critic": {"content_score": 92, "summary": "Lengkap.", "defects": []},
            "accept": {
                "accepted": True,
                "content_score": 92,
                "missing_content": [],
                "reasons": [],
                "client_summary": "Memenuhi brief sejarah.",
            },
        },
    )
    assert result.status == "completed", result.summary
    assert result.quality_status in {"passed", "passed_with_warnings"}
    artifact = Path(result.artifact_path)
    document = docx.Document(str(artifact))
    dynasty = None
    for table in document.tables:
        cells = tuple(cell.text.strip() for cell in table.rows[0].cells)
        if cells[0] == "Raja":
            dynasty = table
            break
    assert dynasty is not None
    assert "Sanjaya" in dynasty.rows[1].cells[0].text
    paragraphs = [p.text for p in document.paragraphs]
    assert any("Prasasti Canggal" in text for text in paragraphs)
    assert any(text.strip() == "Pendahuluan" for text in paragraphs)
    assert not any("**" in text for text in paragraphs)
    assert not any(text.strip().startswith("|") for text in paragraphs)
    footer_xml = document.sections[0].footer.paragraphs[0]._p.xml
    assert "PAGE" in footer_xml
    core = document.core_properties
    assert core.title == "Kerajaan Mataram Kuno"
    defects, structural_score = validate_structure("docx", artifact, "bench-a")
    assert structural_score >= 95.0
    if CAPABILITIES.pdf_to_images:
        assert Path(result.preview_path).is_file()
        assert result.scores.visual is not None and result.scores.visual >= 80.0


@pytest.mark.asyncio
async def test_benchmark_b_educational_pptx_deck(tmp_path: Path) -> None:
    result = await run_benchmark(
        tmp_path,
        "pptx",
        "benchmark-b/siklus-air.pptx",
        {
            "objective": "Buat deck edukasi siklus air untuk siswa SMP.",
            "spec": BENCHMARK_PPTX_SPEC,
            "analysis": CLIENT_ANALYSIS_DECK,
            "research": RESEARCH_GENERIC,
            "design": DESIGN_DECK,
            "critic": {"content_score": 91, "summary": "Deck lengkap.", "defects": []},
            "accept": {
                "accepted": True,
                "content_score": 91,
                "missing_content": [],
                "reasons": [],
                "client_summary": "Deck edukatif siap dipakai.",
            },
        },
    )
    assert result.status == "completed", result.summary
    artifact = Path(result.artifact_path)
    presentation = pptx.Presentation(str(artifact))
    assert len(presentation.slides) == 13
    layouts = set()
    native_text_shapes = 0
    charts = 0
    for slide in presentation.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                native_text_shapes += 1
            if getattr(shape, "has_chart", False):
                charts += 1
    assert native_text_shapes >= 40
    assert charts >= 1
    deck_spec = BENCHMARK_PPTX_SPEC
    layouts = {slide["layout"] for slide in deck_spec["slides"]}
    assert len(layouts) >= 10
    background_colors = set()
    for slide in presentation.slides:
        fill = slide.background.fill
        if fill.type is not None:
            try:
                background_colors.add(str(fill.fore_color.rgb))
            except Exception:
                background_colors.add("none")
    assert len(background_colors) >= 1
    preview_path = Path(result.preview_path)
    if preview_path.is_file():
        html_text = preview_path.read_text(encoding="utf-8")
        assert html_text.count('class="slide"') == 13
        assert "Siklus Air" in html_text
    defects, structural_score = validate_structure("pptx", artifact, "bench-b")
    assert structural_score >= 95.0


@pytest.mark.asyncio
async def test_benchmark_c_financial_xlsx_workbook(tmp_path: Path) -> None:
    result = await run_benchmark(
        tmp_path,
        "xlsx",
        "benchmark-c/keuangan.xlsx",
        {
            "objective": "Buat workbook analisis keuangan H1 2026 dengan dashboard.",
            "spec": BENCHMARK_XLSX_SPEC,
            "analysis": CLIENT_ANALYSIS_FINANCE,
            "research": RESEARCH_FINANCE,
            "design": DESIGN_FINANCE,
            "critic": {"content_score": 93, "summary": "Workbook lengkap.", "defects": []},
            "accept": {
                "accepted": True,
                "content_score": 93,
                "missing_content": [],
                "reasons": [],
                "client_summary": "Workbook keuangan sesuai kebutuhan.",
            },
        },
    )
    assert result.status == "completed", result.summary
    artifact = Path(result.artifact_path)
    workbook = openpyxl.load_workbook(artifact, data_only=False)
    assert set(workbook.sheetnames) >= {"Overview", "Transactions", "Analysis", "Dashboard"}
    transactions = workbook["Transactions"]
    assert transactions["C2"].value == 120
    assert transactions["D2"].value == "=SUM($C$2:C2)"
    analysis = workbook["Analysis"]
    assert str(analysis["B2"].value).startswith("=SUMIF")
    dashboard = workbook["Dashboard"]
    assert str(dashboard["B1"].value).startswith("=SUM(")
    assert len(dashboard._charts) >= 1
    assert "AmountColumn" in workbook.defined_names
    assert transactions.freeze_panes == "A2"
    assert len(list(transactions.conditional_formatting)) >= 1
    assert workbook.calculation.fullCalcOnLoad is True
    workbook.close()
    defects, structural_score = validate_structure("xlsx", artifact, "bench-c")
    assert structural_score >= 95.0
