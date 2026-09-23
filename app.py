import base64
import io
import json
import os
import uuid
import zipfile
from google import genai
import streamlit as st

# Tenta carregar o dotenv localmente; no Streamlit Cloud o segredo virá de st.secrets
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configuração da página
st.set_page_config(
    page_title="Gerador de Accordion H5P", page_icon="🗂️", layout="wide"
)

# Recupera chave da API (localmente do .env ou das Secrets do Streamlit Cloud)
api_key = os.getenv("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY")

TXT_BASE64_PATH = "accordion.txt"


def obter_molde_base64() -> str:
    if os.path.exists(TXT_BASE64_PATH):
        with open(TXT_BASE64_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


# Cabeçalho
st.title("Gerador de Accordion H5P")
st.write(
    "Passe um tema e um texto opcional — a IA organiza em painéis expansíveis."
)
st.divider()

col_form, col_info = st.columns([1, 1], gap="large")

with col_form:
    st.caption("CONTEÚDO")

    tema = st.text_input("Tema *", placeholder="ex.: Fotossíntese")

    instrucao = st.text_area(
        "Instrução",
        help="Foco, público, o que priorizar ou evitar.",
        placeholder="ex.: Material de revisão para alunos do 1º ano do EM...",
    )

    texto_fonte = st.text_area(
        "Texto fonte",
        help="Quando presente, o conteúdo sai daqui.",
        placeholder="Cole aqui o texto que deve ser organizado...",
    )

    col1, col2 = st.columns(2)
    with col1:
        num_paineis = st.number_input(
            "Painéis", min_value=1, max_value=15, value=5
        )
    with col2:
        extensao = st.selectbox("Extensão", ["Curto", "Médio", "Longo"], index=1)

    btn_gerar = st.button(
        "Gerar accordion", type="primary", use_container_width=True
    )

with col_info:
    status_container = st.container(border=True)
    with status_container:
        st.subheader("Status da Geração")
        status_area = st.empty()
        status_area.info(
            "Preencha o tema e clique em gerar para criar o pacote H5P."
        )

# Processamento ao clicar no botão
if btn_gerar:
    if not tema.strip():
        st.warning("Por favor, informe o **Tema**.")
    elif not api_key:
        st.error(
            "Chave GEMINI_API_KEY não foi encontrada nas variáveis de ambiente ou Secrets."
        )
    else:
        status_area.text("⏳ Gerando conteúdo com IA...")

        try:
            # 1. Carrega o molde
            b64_string = obter_molde_base64()
            if not b64_string:
                st.error(f"Arquivo '{TXT_BASE64_PATH}' não encontrado.")
                st.stop()

            molde_bytes = base64.b64decode(b64_string)
            molde_buffer = io.BytesIO(molde_bytes)

            # 2. Chama a API do Gemini (forçando a api_version v1)
            client = genai.Client(api_key=api_key, http_options={'api_version': 'v1'})
            prompt = f"""Você é um assistente pedagógico. Crie um conteúdo para a ferramenta Accordion do H5P.
Tema: {tema}
Número de painéis: {num_paineis}
Extensão aproximada: {extensao}
Instruções adicionais: {instrucao}
Texto base: {texto_fonte}

Responda EXCLUSIVAMENTE com um array JSON válido sem marcadores Markdown.
Formato obrigatório:
[
  {{
    "title": "Título do Painel",
    "content": "<p>Conteúdo do painel.</p>"
  }}
]"""

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )

            texto_resposta = response.text.strip()
            if texto_resposta.startswith("```"):
                texto_resposta = (
                    texto_resposta.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                )

            paineis_json = json.loads(texto_resposta)

            # 3. Monta o H5P
            content_h5p = {
                "panels": [
                    {
                        "title": p.get("title", f"Painel {i+1}"),
                        "content": {
                            "params": {"text": p.get("content", "")},
                            "library": "H5P.AdvancedText 1.1",
                            "subContentId": str(uuid.uuid4()),
                            "metadata": {
                                "contentType": "Text",
                                "license": "U",
                                "title": p.get("title", "Untitled Text")[:60],
                            },
                        },
                    }
                    for i, p in enumerate(paineis_json)
                ],
                "hTag": "h2",
            }

            output_buffer = io.BytesIO()
            with zipfile.ZipFile(molde_buffer, "r") as zip_in:
                with zipfile.ZipFile(
                    output_buffer, "w", zipfile.ZIP_DEFLATED
                ) as zip_out:
                    for item in zip_in.infolist():
                        if item.filename == "content/content.json":
                            zip_out.writestr(
                                "content/content.json",
                                json.dumps(
                                    content_h5p, ensure_ascii=False, indent=2
                                ),
                            )
                        elif item.filename == "h5p.json":
                            try:
                                h5p_data = json.loads(
                                    zip_in.read(item.filename).decode("utf-8")
                                )
                                h5p_data["title"] = tema
                                zip_out.writestr(
                                    "h5p.json",
                                    json.dumps(
                                        h5p_data, ensure_ascii=False, indent=2
                                    ),
                                )
                            except Exception:
                                zip_out.writestr(
                                    item.filename, zip_in.read(item.filename)
                                )
                        else:
                            zip_out.writestr(
                                item.filename, zip_in.read(item.filename)
                            )

            output_buffer.seek(0)
            nome_sanitizado = "".join(
                c for c in tema if c.isalnum() or c in (" ", "_")
            ).rstrip()
            nome_arquivo = f"accordion_{nome_sanitizado}.h5p"

            status_area.success(
                "✅ Arquivo gerado com sucesso! Clique no botão abaixo para baixar."
            )

            # Botão de Download do Streamlit
            st.download_button(
                label="📥 Baixar arquivo .h5p",
                data=output_buffer.getvalue(),
                file_name=nome_arquivo,
                mime="application/zip",
                type="primary",
            )

        except Exception as e:
            status_area.error(f"Erro no processamento: {str(e)}")
