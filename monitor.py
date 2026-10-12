#!/usr/bin/env python3
"""
Monitor de preços Viagogo | Olivia Rodrigo

Abre cada página de concerto num browser real (Playwright), lê o preço mais
baixo para a quantidade definida e envia uma notificação para o telemóvel
(ntfy e/ou Telegram) quando o preço fica igual ou abaixo do limite.

Modos:
  python monitor.py                 corre em ciclo contínuo
  python monitor.py --uma-vez       corre um único ciclo e termina
  python monitor.py --abrir         abre o browser para aceitar cookies / escolher EUR
  python monitor.py --calibrar 0    analisa o concerto n.º 0 e guarda captura + diagnóstico
  python monitor.py --testar-notificacao
"""
import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

PASTA = Path(__file__).resolve().parent
FICHEIRO_CONFIG = PASTA / "config.json"
FICHEIRO_ESTADO = PASTA / "estado.json"
PASTA_PERFIL = PASTA / "perfil_browser"

# Textos que indicam que a Viagogo bloqueou o acesso
PALAVRAS_BLOQUEIO = [
    "captcha", "access denied", "verify you are human", "are you a robot",
    "pardon our interruption", "unusual traffic", "acesso negado",
]

# Número com separadores de milhares (ponto, vírgula, espaço não separável) e decimais opcionais
_NUM = r"\d{1,3}(?:[.,\u00a0\u202f]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_RE_EUR = re.compile(rf"(?:€|EUR)\s*({_NUM})|({_NUM})\s*(?:€|EUR)", re.IGNORECASE)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- preços

def converter_numero(bruto):
    """'1.234,56' -> 1234.56 | '1,234.56' -> 1234.56 | '1.234' -> 1234 | '180,5' -> 180.5"""
    bruto = re.sub(r"[\u00a0\u202f ]", "", bruto)
    sep = max(bruto.rfind("."), bruto.rfind(","))
    if sep == -1:
        return float(bruto)
    decimais = bruto[sep + 1:]
    if len(decimais) in (1, 2):
        inteiro = re.sub(r"[.,]", "", bruto[:sep])
        return float(f"{inteiro}.{decimais}")
    return float(re.sub(r"[.,]", "", bruto))


def extrair_valores_eur(texto):
    """Devolve todos os valores em euros encontrados num texto."""
    valores = []
    for m in _RE_EUR.finditer(texto or ""):
        bruto = m.group(1) or m.group(2)
        try:
            valores.append(converter_numero(bruto))
        except ValueError:
            pass
    return valores


# ---------------------------------------------------------------- lógica de alertas

def avaliar(estado_c, resultado, limite, alertar_falhas_apos):
    """
    Atualiza o estado de um concerto e devolve (novo_estado, lista_de_mensagens).
    Regras:
      - preço <= limite e ainda não alertado          -> alerta
      - preço <= limite e desceu desde o último alerta -> novo alerta
      - preço > limite                                 -> limpa o alerta (volta a avisar se descer)
      - N leituras seguidas sem preço                  -> um aviso de falha (até recuperar)
    """
    e = dict(estado_c or {})
    e.setdefault("alertado_em", None)
    e.setdefault("falhas", 0)
    e.setdefault("falha_avisada", False)
    msgs = []

    if resultado["estado"] != "ok":
        e["falhas"] += 1
        if e["falhas"] >= alertar_falhas_apos and not e["falha_avisada"]:
            e["falha_avisada"] = True
            msgs.append(("falha", resultado["estado"]))
        return e, msgs

    preco = resultado["valor_comparado"]
    if e["falha_avisada"]:
        msgs.append(("recuperado", preco))
    e["falhas"] = 0
    e["falha_avisada"] = False
    e["ultimo_preco"] = preco

    if preco <= limite:
        if e["alertado_em"] is None or preco < e["alertado_em"] - 0.01:
            msgs.append(("preco", preco))
            e["alertado_em"] = preco
    else:
        e["alertado_em"] = None
    return e, msgs


# ---------------------------------------------------------------- notificações

def _post(url, dados=None, cabecalhos=None):
    req = urllib.request.Request(url, data=dados, headers=cabecalhos or {}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def notificar(cfg, titulo, texto, link=None, prioridade="high"):
    n = cfg.get("notificacoes", {})
    enviado = False
    # No GitHub, os dados privados vêm dos "secrets" (variáveis de ambiente)
    topico = (os.environ.get("NTFY_TOPICO") or n.get("ntfy_topico") or "").strip()
    if topico:
        servidor = (n.get("ntfy_servidor") or "https://ntfy.sh").rstrip("/")
        params = {"title": titulo, "priority": prioridade, "tags": "ticket"}
        if link:
            params["click"] = link
        url = f"{servidor}/{urllib.parse.quote(topico)}?{urllib.parse.urlencode(params)}"
        try:
            _post(url, texto.encode("utf-8"))
            enviado = True
        except Exception as ex:
            log(f"Erro ntfy: {ex}")
    token = (os.environ.get("TELEGRAM_TOKEN") or n.get("telegram_token") or "").strip()
    chat = str(os.environ.get("TELEGRAM_CHAT_ID") or n.get("telegram_chat_id") or "").strip()
    if token and chat:
        corpo = f"{titulo}\n{texto}" + (f"\n{link}" if link else "")
        try:
            _post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json.dumps({"chat_id": chat, "text": corpo, "disable_web_page_preview": True}).encode("utf-8"),
                  {"Content-Type": "application/json"})
            enviado = True
        except Exception as ex:
            log(f"Erro Telegram: {ex}")
    if not enviado:
        log(f"(sem notificação configurada) {titulo} | {texto}")


# ---------------------------------------------------------------- browser

def montar_url(url, parametros):
    p = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(p.query))
    q.update({k: str(v) for k, v in (parametros or {}).items()})
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), p.fragment))


# Recolhe textos visíveis com euros. Marca os que estão dentro de um anúncio (listing).
JS_CANDIDATOS = r"""
(seletor) => {
  const visivel = el => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || +st.opacity === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const out = [];
  if (seletor) {
    document.querySelectorAll(seletor).forEach(el => {
      if (visivel(el)) out.push({texto: el.innerText, contexto: el.innerText.slice(0, 140), anuncio: true});
    });
    return out;
  }
  const vistos = new Set();
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    const t = w.currentNode.nodeValue || '';
    if (!/€|EUR/i.test(t)) continue;
    let el = w.currentNode.parentElement;
    if (!el || el.closest('input,select,option,[role="slider"],script,style,noscript,s,del')) continue;
    // "€" e o número podem estar em nós separados: subir até ao elemento que tem ambos
    for (let i = 0; i < 3 && el && !/\d/.test(el.innerText || ''); i++) el = el.parentElement;
    if (!el || vistos.has(el) || !visivel(el)) continue;
    vistos.add(el);
    const txt = (el.innerText || '').trim();
    if (txt.length > 60) continue;
    const anuncio = !!el.closest('[data-listing-id],[data-testid*="listing" i],[class*="listing" i],[id*="listing" i]');
    const ctxEl = el.closest('[data-listing-id],[data-testid*="listing" i],li,article,tr') || el.parentElement || el;
    out.push({texto: txt, contexto: (ctxEl.innerText || '').replace(/\s+/g, ' ').slice(0, 140), anuncio});
  }
  return out;
}
"""


async def ler_preco(pagina, url, seletor=""):
    try:
        await pagina.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await pagina.wait_for_function("document.body && /€|EUR/i.test(document.body.innerText)", timeout=25000)
        except Exception:
            pass
        await pagina.wait_for_timeout(random.randint(3000, 5000))
        corpo = (await pagina.inner_text("body")).lower()
        if any(p in corpo for p in PALAVRAS_BLOQUEIO):
            return {"estado": "bloqueado", "preco": None, "candidatos": []}
        candidatos = await pagina.evaluate(JS_CANDIDATOS, seletor or "")
    except Exception as ex:
        return {"estado": f"erro: {str(ex).splitlines()[0][:120]}", "preco": None, "candidatos": []}

    for c in candidatos:
        c["valores"] = extrair_valores_eur(c["texto"])
    de_anuncios = [v for c in candidatos if c["anuncio"] for v in c["valores"]]
    todos = [v for c in candidatos for v in c["valores"]]
    valores = de_anuncios or todos
    valores = [v for v in valores if v >= 1]
    if not valores:
        return {"estado": "sem preços (esgotado, moeda não EUR ou seletor desatualizado)", "preco": None, "candidatos": candidatos}
    return {"estado": "ok", "preco": min(valores), "candidatos": candidatos}


async def abrir_contexto(pw, cfg):
    PASTA_PERFIL.mkdir(exist_ok=True)
    opcoes = dict(
        user_data_dir=str(PASTA_PERFIL),
        headless=os.environ.get("HEADLESS", str(cfg.get("headless", False))).lower() in ("1", "true", "sim"),
        locale="pt-PT",
        timezone_id="Europe/Lisbon",
        viewport={"width": 1366, "height": 900},
    )
    try:
        return await pw.chromium.launch_persistent_context(channel="chrome", **opcoes)
    except Exception:
        return await pw.chromium.launch_persistent_context(**opcoes)


# ---------------------------------------------------------------- ciclo

def carregar_json(caminho, omissao):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return omissao


def guardar_estado(estado):
    FICHEIRO_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


def eur(v):
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


async def correr_ciclo(pagina, cfg, estado):
    qtd = int(cfg.get("quantidade", 2))
    por_total = cfg.get("limite_por", "bilhete") == "total"
    resumo = []
    for i, c in enumerate(cfg["concertos"]):
        nome = c.get("nome") or f"Concerto {i}"
        url = montar_url(c["url"], cfg.get("parametros_url", {}))
        limite = float(c.get("limite_eur", cfg["limite_eur"]))
        r = await ler_preco(pagina, url, cfg.get("seletor_preco", ""))
        if r["estado"] == "ok":
            r["valor_comparado"] = r["preco"] * qtd if por_total else r["preco"]
        estado[nome], msgs = avaliar(estado.get(nome), r, limite, int(cfg.get("alertar_falhas_apos", 3)))

        if r["estado"] == "ok":
            log(f"{nome}: {eur(r['preco'])}/bilhete" + (f" ({eur(r['valor_comparado'])} total)" if por_total else ""))
            resumo.append((r["valor_comparado"], nome))
        else:
            log(f"{nome}: {r['estado']}")

        unidade = f"total ({qtd} bilhetes)" if por_total else "por bilhete"
        for tipo, valor in msgs:
            if tipo == "preco":
                notificar(cfg, f"Bilhete abaixo do limite: {nome}",
                          f"{eur(valor)} {unidade} (limite {eur(limite)}). Confirma o preço final no checkout.", c["url"], "urgent")
            elif tipo == "falha":
                notificar(cfg, f"Monitor sem leitura: {nome}",
                          f"{estado[nome]['falhas']} tentativas seguidas falharam: {valor}", c["url"], "default")
            elif tipo == "recuperado":
                notificar(cfg, f"Monitor recuperou: {nome}", f"Preço atual {eur(valor)} {unidade}", c["url"], "low")
        guardar_estado(estado)
        if i < len(cfg["concertos"]) - 1:
            pausa = cfg.get("pausa_entre_concertos_seg", [4, 9])
            await asyncio.sleep(random.uniform(pausa[0], pausa[1]))
    if resumo:
        v, n = min(resumo)
        log(f"Mais barato neste ciclo: {n} | {eur(v)}")


async def principal(args):
    cfg = carregar_json(FICHEIRO_CONFIG, None)
    if cfg is None:
        sys.exit(f"Falta o ficheiro {FICHEIRO_CONFIG}")

    if args.testar_notificacao:
        notificar(cfg, "Teste do monitor Viagogo", "Se estás a ler isto no telemóvel, as notificações funcionam.")
        return

    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        contexto = await abrir_contexto(pw, cfg)
        pagina = contexto.pages[0] if contexto.pages else await contexto.new_page()

        if args.abrir:
            await pagina.goto("https://www.viagogo.pt")
            input("Aceita os cookies e confirma que a moeda está em EUR. Depois carrega Enter aqui para fechar... ")
            await contexto.close()
            return

        if args.calibrar is not None:
            c = cfg["concertos"][args.calibrar]
            url = montar_url(c["url"], cfg.get("parametros_url", {}))
            r = await ler_preco(pagina, url, cfg.get("seletor_preco", ""))
            await pagina.screenshot(path=str(PASTA / "calibracao.png"), full_page=True)
            (PASTA / "calibracao.html").write_text(await pagina.content(), encoding="utf-8")
            print(f"\nURL: {url}\nEstado: {r['estado']}\nPreço mínimo lido: {r['preco']}\n")
            print("Textos com euros encontrados (A = dentro de anúncio):")
            for cand in r["candidatos"][:60]:
                print(f"  {'A' if cand['anuncio'] else ' '} {cand.get('valores')}  |  {cand['texto']!r}  |  {cand['contexto']}")
            print("\nGuardado: calibracao.png e calibracao.html")
            await contexto.close()
            return

        intervalo = float(cfg.get("intervalo_minutos", 10)) * 60
        estado = carregar_json(FICHEIRO_ESTADO, {})
        # No modo --uma-vez (GitHub) não avisa o arranque, senão chegava uma notificação a cada execução
        if not args.uma_vez:
            notificar(cfg, "Monitor Viagogo iniciado",
                      f"{len(cfg['concertos'])} concertos | limite {eur(float(cfg['limite_eur']))} | a cada {cfg.get('intervalo_minutos', 10)} min",
                      prioridade="low")
        while True:
            inicio = time.monotonic()
            log("Início do ciclo")
            await correr_ciclo(pagina, cfg, estado)
            if args.uma_vez:
                break
            espera = max(30, intervalo - (time.monotonic() - inicio) + random.uniform(-45, 45))
            log(f"Próximo ciclo em {espera / 60:.1f} min")
            await asyncio.sleep(espera)
        await contexto.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Monitor de preços Viagogo")
    ap.add_argument("--uma-vez", action="store_true")
    ap.add_argument("--abrir", action="store_true")
    ap.add_argument("--calibrar", type=int, metavar="N")
    ap.add_argument("--testar-notificacao", action="store_true")
    try:
        asyncio.run(principal(ap.parse_args()))
    except KeyboardInterrupt:
        log("Parado pelo utilizador")
