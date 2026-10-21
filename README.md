# Census Comparison Agent

    census_project/
      backend/
        main.py                 pipeline + CLI        api.py  optional FastAPI endpoint
        config.py               paths, template cell map, colours, business constants
        readers.py              pdf (digital + PaddleOCR), xlsx, xls, csv, docx -> common structure
        classifier.py           header/field mapping + document role (census/invoice/renewal/ghq/rfp)
        census_builder.py       multi-file employee + dependent consolidation, standardisation, validation
        prospect.py             GHQ (primary) / RFP (supplement) -> prospect block
        invoice_parser.py       invoice rows, billing period, latest/revised selection, multi-entity combine
        renewal_parser.py       renewal rate tables, Final/Revised/latest + future-period selection
        comparator.py           matching, tier validation, WP/NE/WO rules, most-used-plan, premiums
        workbook_writer.py      fills the template (A-L untouched) + Summary sheet
        llm_client.py           Claude Anthropic extraction and classification (claude-opus-5-5 via ANTHROPIC_API_KEY)
        instructions/           Census_Comparison_REI.docx (the rule book this code implements)
        templates/              RAPT_Census_-_Template.xlsx (master output format)
      input/   output/   tests/make_sample_data.py   requirements.txt

Run:  `pip install -r requirements.txt` then `python backend/main.py`
(drop every uploaded file in `input/`; the workbook + a short `_exceptions.txt` land in `output/`).

Web UI & API Server:
- Launch: `python run_server.py` (or `cd backend && uvicorn api:app --reload`)
- Interactive Web UI: `http://127.0.0.1:8000/`
- Interactive Swagger UI: `http://127.0.0.1:8000/swagger` or `http://127.0.0.1:8000/docs`
- Alternative ReDoc API Docs: `http://127.0.0.1:8000/redoc`
- OpenAPI Specification Schema: `http://127.0.0.1:8000/openapi.json` (or `swagger.json`)

OCR Engine (Scanned PDFs):
- PaddleOCR (`paddlepaddle==2.6.2`, `paddleocr>=2.7.0,<=2.8.1`)
- Poppler for PDF page rendering (`POPPLER_PATH` in `.env`)

LLM / Model:
Configured in `.env` with:
- `ANTHROPIC_API_KEY`: Anthropic Claude API key
- `ANTHROPIC_MODEL`: `claude-opus-5-5`

