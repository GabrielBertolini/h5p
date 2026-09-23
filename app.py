import base64
import html
import io
import json
import os
import time
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

# Configuração da página (layout centralizado)
st.set_page_config(page_title="Gerador de Accordion H5P", layout="centered")

# Recupera chave da API (localmente do .env ou das Secrets do Streamlit Cloud)
api_key = os.getenv("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY")

TXT_BASE64_PATH = "accordion.txt"

MODELOS_PARA_TENTAR = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
]

# Anexos
TIPOS_ANEXO = ["pdf", "txt", "md", "csv", "docx", "png", "jpg", "jpeg", "webp"]
MIME_BINARIOS = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
LIMITE_ANEXOS_MB = 18  # limite de envio direto para a API do Gemini (~20 MB)


class ErroAmigavel(Exception):
    """Erro cuja mensagem já está pronta para ser mostrada ao usuário."""


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


def decodificar_texto(dados: bytes) -> str:
    for codificacao in ("utf-8", "latin-1"):
        try:
            return dados.decode(codificacao)
        except UnicodeDecodeError:
            continue
    return dados.decode("utf-8", errors="replace")


def extrair_docx(dados: bytes, nome: str) -> str:
    try:
        import docx  # pacote python-docx
    except ImportError:
        raise ErroAmigavel(
            f"Não consegui ler o arquivo **{nome}**: o suporte a Word (.docx) "
            "não está instalado. Envie o arquivo em PDF ou adicione "
            "`python-docx` ao requirements.txt."
        )
    documento = docx.Document(io.BytesIO(dados))
    linhas = [p.text for p in documento.paragraphs if p.text.strip()]
    for tabela in documento.tables:
        for linha in tabela.rows:
            linhas.append(" | ".join(c.text.strip() for c in linha.cells))
    return "\n".join(linhas)


def preparar_anexos(arquivos):
    """Devolve (partes_binarias, textos) prontos para enviar ao Gemini."""
    partes, textos = [], []
    total = sum(len(a.getvalue()) for a in arquivos or [])
    if total > LIMITE_ANEXOS_MB * 1024 * 1024:
        raise ErroAmigavel(
            f"Os anexos somam {total / 1024 / 1024:.1f} MB, e o limite é "
            f"{LIMITE_ANEXOS_MB} MB. Tente enviar menos arquivos ou versões menores."
        )

    for arq in arquivos or []:
        dados = arq.getvalue()
        ext = arq.name.rsplit(".", 1)[-1].lower()
        try:
            if ext in MIME_BINARIOS:
                partes.append(
                    types.Part.from_bytes(data=dados, mime_type=MIME_BINARIOS[ext])
                )
            elif ext == "docx":
                textos.append((arq.name, extrair_docx(dados, arq.name)))
            else:
                textos.append((arq.name, decodificar_texto(dados)))
        except ErroAmigavel:
            raise
        except Exception:
            raise ErroAmigavel(
                f"Não consegui ler o arquivo **{arq.name}**. Ele pode estar "
                "corrompido ou protegido por senha. Tente removê-lo ou enviar em outro formato."
            )
    return partes, textos


def erro_temporario(err: Exception) -> bool:
    txt = str(err).lower()
    return any(
        s in txt
        for s in ("503", "unavailable", "overloaded", "high demand",
                  "500", "internal", "429", "resource_exhausted")
    )


def chamar_gemini(client, contents) -> str:
    """Tenta os modelos em sequência; se todos estiverem sobrecarregados,
    espera um pouco e tenta mais uma rodada."""
    ultimo_erro = None
    for rodada in range(2):
        for mod in MODELOS_PARA_TENTAR:
            try:
                response = client.models.generate_content(
                    model=mod,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    ),
                )
                if response and response.text:
                    return response.text
            except Exception as err:
                ultimo_erro = err
        if rodada == 0 and ultimo_erro is not None and erro_temporario(ultimo_erro):
            time.sleep(4)
        else:
            break
    raise ultimo_erro or RuntimeError("A IA devolveu uma resposta vazia.")


def gerar_paineis_ia(tema, num_paineis, extensao, instrucao, texto_fonte, anexos) -> list:
    partes_anexos, textos_anexos = preparar_anexos(anexos)

    bloco_anexos = ""
    if textos_anexos:
        bloco_anexos += "\n\nConteúdo dos anexos de texto:\n"
        for nome, texto in textos_anexos:
            bloco_anexos += f"\n### {nome}\n{texto}\n"
    if partes_anexos:
        bloco_anexos += (
            "\n\nTambém foram anexados arquivos (PDFs/imagens) junto desta mensagem. "
            "Use-os como material de apoio."
        )

    prompt = f"""Você é um assistente pedagógico. Crie um conteúdo para a ferramenta Accordion do H5P.
Tema: {tema}
Número de painéis: {num_paineis}
Extensão aproximada: {extensao}
Instruções adicionais: {instrucao}
Texto base: {texto_fonte}{bloco_anexos}

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

    client = genai.Client(api_key=api_key)
    texto_resposta = chamar_gemini(client, [prompt, *partes_anexos])

    dados = json.loads(texto_resposta)
    # Às vezes o modelo devolve {"panels": [...]} em vez da lista direta
    if isinstance(dados, dict):
        dados = next((v for v in dados.values() if isinstance(v, list)), [])
    if not dados:
        raise ErroAmigavel(
            "A IA não devolveu nenhum painel desta vez. Tente gerar de novo, "
            "talvez com uma instrução um pouco mais detalhada."
        )
    return dados


def mensagem_amigavel(err: Exception) -> str:
    """Traduz erros técnicos em mensagens humanas."""
    if isinstance(err, ErroAmigavel):
        return str(err)
    if isinstance(err, json.JSONDecodeError):
        return (
            "A IA respondeu num formato inesperado. Clique em **Gerar accordion** "
            "de novo; normalmente funciona na segunda tentativa."
        )

    txt = str(err).lower()
    if any(s in txt for s in ("503", "unavailable", "overloaded", "high demand")):
        return (
            "A IA está com muita demanda neste momento e não conseguiu responder. "
            "Isso costuma passar rápido: aguarde alguns segundos e tente de novo."
        )
    if any(s in txt for s in ("429", "resource_exhausted", "quota", "rate limit")):
        return "Atingimos o limite de uso da IA por agora. Espere um minuto e tente novamente."
    if any(s in txt for s in ("api key", "api_key", "401", "403", "permission", "unauthenticated")):
        return (
            "Não consegui me conectar à IA porque a chave de acesso parece inválida. "
            "Confira a GEMINI_API_KEY nas configurações do app."
        )
    if "404" in txt or "not found" in txt:
        return (
            "Os modelos de IA configurados não estão disponíveis no momento. "
            "Verifique os nomes dos modelos no código."
        )
    if any(s in txt for s in ("too large", "exceeds", "payload", "token count")):
        return (
            "O material enviado é grande demais para a IA processar de uma vez. "
            "Tente reduzir o texto fonte ou enviar menos anexos."
        )
    if any(s in txt for s in ("timeout", "timed out", "connection", "network")):
        return "Não consegui me conectar à IA. Verifique a conexão e tente novamente."
    return "Algo deu errado ao gerar o conteúdo. Tente novamente em instantes."


def md_para_html(texto: str) -> str:
    # "<" é escapado para que HTML/scripts colados no texto apareçam como texto
    # (o conteúdo continua aceitando toda a formatação Markdown).
    texto = (texto or "").replace("<", "&lt;")
    return markdown.markdown(texto, extensions=["extra", "nl2br", "sane_lists"])


def nome_arquivo(titulo: str) -> str:
    nome = "".join(c for c in titulo if c.isalnum() or c in (" ", "_")).strip()
    return nome or "accordion"


# ---------------------------------------------------------------------------
# Prévia HTML (imita o H5P Accordion para quem não tem Lumi/Moodle)
# ---------------------------------------------------------------------------
PREVIA_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITULO__ · Prévia</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px 40px;
    background: #eef1f4; color: #212121;
    font-family: "Open Sans", "Segoe UI", Arial, sans-serif; font-size: 16px; line-height: 1.5;
  }
  .barra {
    max-width: 820px; margin: 0 auto 14px; display: flex; gap: 12px;
    align-items: center; justify-content: space-between; flex-wrap: wrap;
    font-size: 13px; color: #5b6570;
  }
  .barra button {
    font: inherit; font-weight: 600; color: #fff; background: #1a73d9;
    border: 0; border-radius: 6px; padding: 7px 14px; cursor: pointer;
  }
  .barra button:hover { background: #1560b5; }
  .aviso-bloqueio { display: none; color: #b3261e; width: 100%; }
  .h5p-frame {
    max-width: 820px; margin: 0 auto; background: #fff;
    border: 1px solid #d7dde3; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,.06);
  }
  .h5p-frame > h1 {
    margin: 0; padding: 18px 20px 14px; font-size: 22px; font-weight: 600;
    border-bottom: 1px solid #e3e7eb;
  }
  .h5p-accordion { padding: 16px 20px 20px; }
  .h5p-panel { border: 1px solid #dbe2e8; border-radius: 3px; margin-bottom: 6px; overflow: hidden; }
  .h5p-panel h2 { margin: 0; font-size: 1em; }
  .h5p-panel-title {
    position: relative; width: 100%; text-align: left; cursor: pointer;
    font: inherit; font-weight: 600; color: #212121;
    background: #f5f7f9; border: 0; padding: 12px 16px 12px 46px;
    transition: background .15s;
  }
  .h5p-panel-title:hover { background: #e9edf1; }
  .h5p-panel-title:focus-visible { outline: 2px solid #1a73d9; outline-offset: -2px; }
  .h5p-panel-title::before {
    content: "+"; position: absolute; left: 16px; top: 50%; transform: translateY(-50%);
    width: 20px; height: 20px; line-height: 18px; text-align: center; border-radius: 50%;
    font-weight: 700; font-size: 16px; color: #fff; background: #1a73d9;
  }
  .h5p-panel-title[aria-expanded="true"] { background: #e3ebf5; color: #0f4c96; }
  .h5p-panel-title[aria-expanded="true"]::before { content: "\\2212"; }
  .h5p-panel-content {
    display: grid; grid-template-rows: 0fr; transition: grid-template-rows .25s ease;
  }
  .h5p-panel-content.aberto { grid-template-rows: 1fr; }
  .h5p-panel-content > div { overflow: hidden; }
  .h5p-panel-content .texto { padding: 12px 18px 14px 46px; border-top: 1px solid #dbe2e8; }
  .texto p { margin: 0 0 .8em; }
  .texto p:last-child, .texto ul:last-child, .texto ol:last-child { margin-bottom: 0; }
  .texto ul, .texto ol { margin: 0 0 .8em; padding-left: 1.3em; }
  .texto table { border-collapse: collapse; }
  .texto td, .texto th { border: 1px solid #dbe2e8; padding: 4px 8px; }
  .rodape {
    padding: 8px 20px; border-top: 1px solid #e3e7eb; font-size: 12px; color: #8a949e;
  }
  @media (prefers-reduced-motion: reduce) { .h5p-panel-content { transition: none; } }
</style>
</head>
<body>
__BARRA__
<div class="h5p-frame">
  <h1>__TITULO__</h1>
  <div class="h5p-accordion">
__PAINEIS__
  </div>
  <div class="rodape">Prévia interativa. Imita o Accordion do H5P para testes no navegador.</div>
</div>
<script>
(function () {
  var botoes = Array.prototype.slice.call(document.querySelectorAll(".h5p-panel-title"));
  function fechar(b) {
    b.setAttribute("aria-expanded", "false");
    document.getElementById(b.getAttribute("aria-controls")).classList.remove("aberto");
  }
  botoes.forEach(function (b, i) {
    b.addEventListener("click", function () {
      var aberto = b.getAttribute("aria-expanded") === "true";
      botoes.forEach(fechar);  // como no H5P: só um painel aberto por vez
      if (!aberto) {
        b.setAttribute("aria-expanded", "true");
        document.getElementById(b.getAttribute("aria-controls")).classList.add("aberto");
      }
    });
    b.addEventListener("keydown", function (e) {
      var alvo = null;
      if (e.key === "ArrowDown") alvo = botoes[(i + 1) % botoes.length];
      if (e.key === "ArrowUp") alvo = botoes[(i - 1 + botoes.length) % botoes.length];
      if (e.key === "Home") alvo = botoes[0];
      if (e.key === "End") alvo = botoes[botoes.length - 1];
      if (alvo) { e.preventDefault(); alvo.focus(); }
    });
  });
})();
</script>
__SCRIPT_EXTRA__
</body>
</html>
"""

BARRA_NOVA_ABA = """<div class="barra">
  <span>Clique nos títulos para abrir e fechar os painéis.</span>
  <button type="button" id="abrir-aba">Abrir em nova aba ↗</button>
  <span class="aviso-bloqueio" id="aviso-bloqueio">O navegador bloqueou a nova aba.
  Use o botão "Baixar prévia (.html)" abaixo.</span>
</div>"""

SCRIPT_NOVA_ABA = """<script>
document.getElementById("abrir-aba").addEventListener("click", function () {
  var url = URL.createObjectURL(new Blob([PAGINA], {type: "text/html"}));
  var janela = window.open(url, "_blank");
  if (!janela) document.getElementById("aviso-bloqueio").style.display = "block";
});
var PAGINA = __PAGINA_JSON__;
</script>"""


def gerar_html_previa(titulo_geral: str, paineis: list, com_botao_nova_aba=False) -> str:
    """Página HTML independente que imita o H5P Accordion."""
    blocos = []
    for i, p in enumerate(paineis):
        titulo = html.escape(p["title"] or f"Painel {i+1}")
        blocos.append(
            f'    <div class="h5p-panel">\n'
            f'      <h2><button class="h5p-panel-title" id="t{i}" aria-expanded="false" '
            f'aria-controls="c{i}">{titulo}</button></h2>\n'
            f'      <div class="h5p-panel-content" id="c{i}" role="region" aria-labelledby="t{i}">'
            f'<div><div class="texto">{md_para_html(p["content"])}</div></div></div>\n'
            f"    </div>"
        )

    pagina = (
        PREVIA_TEMPLATE
        .replace("__TITULO__", html.escape(titulo_geral or "Accordion"))
        .replace("__PAINEIS__", "\n".join(blocos))
    )
    pagina_limpa = pagina.replace("__BARRA__", "").replace("__SCRIPT_EXTRA__", "")
    if not com_botao_nova_aba:
        return pagina_limpa

    pagina_json = json.dumps(pagina_limpa).replace("</", "<\\/")
    return (
        pagina.replace("__BARRA__", BARRA_NOVA_ABA)
        .replace("__SCRIPT_EXTRA__", SCRIPT_NOVA_ABA.replace("__PAGINA_JSON__", pagina_json))
    )


def mostrar_html(conteudo_html: str, altura: int):
    """Mostra HTML interativo dentro do app (compatível com versões novas e antigas)."""
    if hasattr(st, "iframe"):
        st.iframe(conteudo_html, height=altura)
    else:
        import streamlit.components.v1 as components
        components.html(conteudo_html, height=altura, scrolling=True)


def montar_h5p(titulo_geral: str, paineis: list) -> bytes:
    """paineis: lista de dicts {"title": str, "content": str (markdown)}."""
    b64_string = obter_molde_base64()
    if not b64_string:
        raise ErroAmigavel(
            f"Não encontrei o molde do accordion ('{TXT_BASE64_PATH}'). "
            "Verifique se o arquivo está no repositório."
        )
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
                        h5p_data["extraTitle"] = titulo_geral
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
# Interface: formulário
# ---------------------------------------------------------------------------
st.title("Gerador de Accordion H5P", anchor=False)
st.divider()

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

anexos = st.file_uploader(
    "Anexos (opcional)",
    type=TIPOS_ANEXO,
    accept_multiple_files=True,
    help="PDFs, textos, documentos Word ou imagens para complementar o conteúdo. "
    f"Até {LIMITE_ANEXOS_MB} MB no total.",
)

col1, col2 = st.columns(2)
with col1:
    num_paineis = st.number_input("Painéis", min_value=1, max_value=15, value=5)
with col2:
    extensao = st.selectbox("Extensão", ["Curto", "Médio", "Longo"], index=1)

btn_gerar = st.button("Gerar accordion", type="primary", use_container_width=True)

# Status logo abaixo do botão
status_area = st.empty()
if st.session_state.get("paineis") is not None:
    status_area.success("Pronto! Revise e edite o accordion abaixo.")

# Processamento ao clicar em "Gerar accordion"
if btn_gerar:
    if not tema.strip():
        status_area.warning("Para começar, informe o **tema** do accordion.")
    elif not api_key:
        status_area.error(
            "A chave da IA (GEMINI_API_KEY) não está configurada. "
            "Adicione-a nas Secrets do app para poder gerar conteúdo."
        )
    else:
        try:
            with status_area.container():
                with st.spinner("A IA está processando o conteúdo..."):
                    paineis_json = gerar_paineis_ia(
                        tema, num_paineis, extensao, instrucao, texto_fonte, anexos
                    )
            st.session_state.paineis = [
                novo_painel(p.get("title", f"Painel {i+1}"), p.get("content", ""))
                for i, p in enumerate(paineis_json)
            ]
            st.session_state.titulo_geral = tema
            st.session_state.h5p_final = None
            status_area.success("Pronto! Revise e edite o accordion abaixo.")
        except Exception as e:
            with status_area.container():
                st.error(mensagem_amigavel(e))
                if not isinstance(e, ErroAmigavel):
                    with st.expander("Detalhes técnicos"):
                        st.code(str(e), language=None)

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

    titulo_geral, dados = coletar_dados()
    assinatura = json.dumps([titulo_geral, dados], ensure_ascii=False)

    # --- Prévia interativa no navegador ------------------------------------
    st.divider()
    st.subheader("Testar no navegador", anchor=False)
    st.caption(
        "Uma prévia que imita o accordion do H5P, para testar sem Lumi ou Moodle. "
        "Ela sempre mostra o conteúdo atual do editor."
    )
    if dados:
        altura_previa = min(1000, 190 + 52 * len(dados) + 320)
        mostrar_html(
            gerar_html_previa(titulo_geral, dados, com_botao_nova_aba=True),
            altura_previa,
        )
        st.download_button(
            label="Baixar prévia (.html)",
            data=gerar_html_previa(titulo_geral, dados).encode("utf-8"),
            file_name=f"previa_{nome_arquivo(titulo_geral)}.html",
            mime="text/html",
            icon=":material/public:",
            on_click="ignore",
            use_container_width=True,
            help="Abre em qualquer navegador. Dá para enviar para outras pessoas testarem.",
        )
    else:
        st.info("Adicione pelo menos um painel para ver a prévia.")

    st.divider()

    if st.button("Gerar arquivo .h5p final", type="primary", use_container_width=True):
        st.session_state.h5p_final = None
        if not titulo_geral:
            st.warning("Dê um **título geral** ao accordion antes de gerar o arquivo.")
        elif not dados:
            st.warning("Adicione pelo menos um painel.")
        else:
            try:
                st.session_state.h5p_final = {
                    "bytes": montar_h5p(titulo_geral, dados),
                    "nome": f"accordion_{nome_arquivo(titulo_geral)}.h5p",
                    "assinatura": assinatura,
                }
            except Exception as e:
                st.error(
                    mensagem_amigavel(e) if isinstance(e, ErroAmigavel)
                    else "Não consegui montar o arquivo .h5p. Tente novamente."
                )

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
                mime="application/octet-stream",  # evita o navegador acrescentar ".zip"
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
