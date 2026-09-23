import base64
import io
import json
import os
import uuid
import zipfile

import markdown
import streamlit as st
from google import genai
from google.genai import types

# Tenta carregar o dotenv localmente; no Streamlit Cloud o segredo virá de st.secrets
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configuração da página
st.set_page_config(page_title="Gerador de Accordion H5P", layout="wide")

# Recupera chave da API (localmente do .env ou das Secrets do Streamlit Cloud)
api_key = os.getenv("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY")

TXT_BASE64_PATH = "accordion.txt"

MODELOS_PARA_TENTAR = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
]


# ---------------------------------------------------------------------------
# Funções auxiliares
# ---------------------------------------------------------------------------
def obter_molde_base64() -> str:
    if os.path.exists(TXT_BASE64_PATH):
        with open(TXT_BASE64_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def novo_painel(titulo: str = "", conteudo: str = "") -> str:
    """Cria um painel no estado da sessão e devolve seu id."""
    pid = uuid.uuid4().hex[:8]
    st.session_state[f"tit_{pid}"] = titulo
    st.session_state[f"cont_{pid}"] = conteudo
    return pid


def gerar_paineis_ia(tema, num_paineis, extensao, instrucao, texto_fonte) -> list:
    client = genai.Client(api_key=api_key)

    prompt = f"""Você é um assistente pedagógico. Crie um conteúdo para a ferramenta Accordion do H5P.
Tema: {tema}
Número de painéis: {num_paineis}
Extensão aproximada: {extensao}
Instruções adicionais: {instrucao}
Texto base: {texto_fonte}

Gere uma lista JSON válida contendo os painéis.
O campo "content" deve estar em Markdown simples (parágrafos separados por linha em branco,
**negrito**, *itálico* e listas com "-"). Não use HTML.
Formato obrigatório de cada item:
[
  {{
    "title": "Título do Painel",
    "content": "Texto do painel em Markdown."
  }}
]"""

    texto_resposta = None
    ultimo_erro = None
    for mod in MODELOS_PARA_TENTAR:
        try:
            response = client.models.generate_content(
                model=mod,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                ),
            )
            if response and response.text:
                texto_resposta = response.text
                break
        except Exception as err:
            ultimo_erro = err
            continue

    if not texto_resposta:
        raise Exception(f"Erro ao conectar com modelos Gemini: {ultimo_erro}")

    dados = json.loads(texto_resposta)
    # Às vezes o modelo devolve {"panels": [...]} em vez da lista direta
    if isinstance(dados, dict):
        dados = next((v for v in dados.values() if isinstance(v, list)), [])
    return dados


def md_para_html(texto: str) -> str:
    return markdown.markdown(
        texto or "", extensions=["extra", "nl2br", "sane_lists"]
    )


def montar_h5p(titulo_geral: str, paineis: list) -> bytes:
    """paineis: lista de dicts {"title": str, "content": str (markdown)}."""
    b64_string = obter_molde_base64()
    if not b64_string:
        raise FileNotFoundError(f"Arquivo '{TXT_BASE64_PATH}' não encontrado.")
    molde_buffer = io.BytesIO(base64.b64decode(b64_string))

    content_h5p = {
        "panels": [
            {
                "title": p["title"] or f"Painel {i+1}",
                "content": {
                    "params": {"text": md_para_html(p["content"])},
                    "library": "H5P.AdvancedText 1.1",
                    "subContentId": str(uuid.uuid4()),
                    "metadata": {
                        "contentType": "Text",
                        "license": "U",
                        "title": (p["title"] or "Untitled Text")[:60],
                    },
                },
            }
            for i, p in enumerate(paineis)
        ],
        "hTag": "h2",
    }

    output_buffer = io.BytesIO()
    with zipfile.ZipFile(molde_buffer, "r") as zip_in:
        with zipfile.ZipFile(output_buffer, "w", zipfile.ZIP_DEFLATED) as zip_out:
            for item in zip_in.infolist():
                if item.filename == "content/content.json":
                    zip_out.writestr(
                        "content/content.json",
                        json.dumps(content_h5p, ensure_ascii=False, indent=2),
                    )
                elif item.filename == "h5p.json":
                    try:
                        h5p_data = json.loads(zip_in.read(item.filename).decode("utf-8"))
                        h5p_data["title"] = titulo_geral
                        zip_out.writestr(
                            "h5p.json",
                            json.dumps(h5p_data, ensure_ascii=False, indent=2),
                        )
                    except Exception:
                        zip_out.writestr(item.filename, zip_in.read(item.filename))
                else:
                    zip_out.writestr(item.filename, zip_in.read(item.filename))

    return output_buffer.getvalue()


def coletar_dados():
    """Lê o título geral e os painéis atuais do editor."""
    titulo_geral = (st.session_state.get("titulo_geral") or "").strip()
    dados = [
        {
            "title": (st.session_state.get(f"tit_{pid}") or "").strip(),
            "content": st.session_state.get(f"cont_{pid}") or "",
        }
        for pid in st.session_state.get("paineis", [])
    ]
    return titulo_geral, dados


# ---------------------------------------------------------------------------
# Callbacks do editor (rodam antes do próximo render)
# ---------------------------------------------------------------------------
def adicionar_painel():
    st.session_state.paineis.append(novo_painel("Novo painel", ""))


def remover_painel(pid):
    st.session_state.paineis.remove(pid)


def mover_painel(pid, delta):
    lista = st.session_state.paineis
    i = lista.index(pid)
    j = i + delta
    if 0 <= j < len(lista):
        lista[i], lista[j] = lista[j], lista[i]


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
st.title("Gerador de Accordion H5P", anchor=False)
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
        help="Adicionar material de apoio gera resultados mais personalizados.",
        placeholder="Cole aqui o texto para apoio...",
    )

    col1, col2 = st.columns(2)
    with col1:
        num_paineis = st.number_input("Painéis", min_value=1, max_value=15, value=5)
    with col2:
        extensao = st.selectbox("Extensão", ["Curto", "Médio", "Longo"], index=1)

    btn_gerar = st.button("Gerar accordion", type="primary", use_container_width=True)

with col_info:
    with st.container(border=True):
        st.subheader("Status da Geração", anchor=False)
        status_area = st.empty()
        if st.session_state.get("paineis") is not None:
            status_area.success("Conteúdo pronto! Revise e edite abaixo.")
        else:
            status_area.info(
                "Preencha os campos e clique em gerar para criar o Accordion H5P."
            )

# Processamento ao clicar em "Gerar accordion"
if btn_gerar:
    if not tema.strip():
        status_area.warning("Por favor, informe o **Tema**.")
    elif not api_key:
        status_area.error(
            "Chave GEMINI_API_KEY não foi encontrada nas variáveis de ambiente ou Secrets."
        )
    else:
        try:
            with st.spinner("Gerando conteúdo com IA..."):
                paineis_json = gerar_paineis_ia(
                    tema, num_paineis, extensao, instrucao, texto_fonte
                )
            st.session_state.paineis = [
                novo_painel(p.get("title", f"Painel {i+1}"), p.get("content", ""))
                for i, p in enumerate(paineis_json)
            ]
            st.session_state.titulo_geral = tema
            st.session_state.h5p_final = None
            status_area.success("Conteúdo pronto! Revise e edite abaixo.")
        except Exception as e:
            status_area.error(f"Erro no processamento: {str(e)}")

# ---------------------------------------------------------------------------
# Editor
# ---------------------------------------------------------------------------
if st.session_state.get("paineis") is not None:
    st.divider()
    st.subheader("Editar accordion", anchor=False)

    st.text_input("Título geral", key="titulo_geral")
    st.caption(
        "O texto aceita Markdown: **negrito**, *itálico*, listas com `-` "
        "e parágrafos separados por uma linha em branco."
    )

    paineis = st.session_state.paineis
    total = len(paineis)

    for i, pid in enumerate(paineis):
        with st.container(border=True):
            st.text_input(f"Título do painel {i+1}", key=f"tit_{pid}")

            aba_editar, aba_previa = st.tabs(["Editar", "Pré-visualizar"])
            with aba_editar:
                st.text_area(
                    "Conteúdo",
                    key=f"cont_{pid}",
                    height=200,
                    label_visibility="collapsed",
                )
            with aba_previa:
                st.markdown(st.session_state.get(f"cont_{pid}") or "_(vazio)_")

            b1, b2, b3 = st.columns(3)
            b1.button(
                "Subir", key=f"up_{pid}", icon=":material/arrow_upward:",
                on_click=mover_painel, args=(pid, -1),
                disabled=(i == 0), use_container_width=True,
            )
            b2.button(
                "Descer", key=f"down_{pid}", icon=":material/arrow_downward:",
                on_click=mover_painel, args=(pid, 1),
                disabled=(i == total - 1), use_container_width=True,
            )
            b3.button(
                "Remover", key=f"del_{pid}", icon=":material/delete:",
                on_click=remover_painel, args=(pid,),
                disabled=(total == 1), use_container_width=True,
            )

    st.button(
        "Adicionar painel", icon=":material/add:",
        on_click=adicionar_painel, use_container_width=True,
    )

    st.write("")

    titulo_geral, dados = coletar_dados()
    assinatura = json.dumps([titulo_geral, dados], ensure_ascii=False)

    if st.button("Gerar arquivo .h5p final", type="primary", use_container_width=True):
        st.session_state.h5p_final = None
        if not titulo_geral:
            st.warning("Informe o **Título geral**.")
        elif not dados:
            st.warning("Adicione pelo menos um painel.")
        else:
            try:
                nome_sanitizado = "".join(
                    c for c in titulo_geral if c.isalnum() or c in (" ", "_")
                ).strip() or "accordion"
                st.session_state.h5p_final = {
                    "bytes": montar_h5p(titulo_geral, dados),
                    "nome": f"accordion_{nome_sanitizado}.h5p",
                    "assinatura": assinatura,
                }
            except Exception as e:
                st.error(f"Erro ao montar o arquivo: {str(e)}")

    # O botão de download é renderizado em TODA execução enquanto o arquivo
    # existir; assim o Streamlit não descarta o arquivo antes do navegador baixá-lo.
    final = st.session_state.get("h5p_final")
    if final:
        if final["assinatura"] == assinatura:
            st.success("Arquivo final gerado!")
            st.download_button(
                label="Baixar arquivo .h5p",
                data=final["bytes"],
                file_name=final["nome"],
                mime="application/zip",
                type="primary",
                icon=":material/download:",
                on_click="ignore",
                use_container_width=True,
            )
        else:
            st.info(
                "Você alterou o conteúdo depois de gerar o arquivo. "
                "Clique em **Gerar arquivo .h5p final** novamente."
            )
