"""
Capturador de Topicos do Twitter/X - implementacao de referencia em Python.

Reproduz em codigo puro a mesma logica do workflow n8n (workflow/Capturador_Topicos_Twitter.json):

1. Recebe a URL de um tweet (pode ser o primeiro, o ultimo ou qualquer tweet no meio de uma thread).
2. Busca os dados publicos do tweet via o endpoint de sindicacao do Twitter/X
   (o mesmo usado para gerar embeds de tweets em sites de terceiros - nao requer
   API key nem login).
3. Segue a cadeia "in_reply_to_status_id_str" para tras, tweet a tweet, enquanto o
   autor da resposta for o mesmo autor do tweet original (thread = auto-resposta
   encadeada). Para assim que encontra o tweet raiz (sem in_reply_to) ou uma
   resposta de outra pessoa.
4. Classifica o resultado como "tweet_unico" ou "thread" e devolve os tweets em
   ordem cronologica (raiz -> tweet informado).
5. Salva o resultado em JSON (dados estruturados) e em Markdown (texto limpo e
   legivel), prontos para arquivar, indexar ou alimentar um resumidor de IA.

Uso:
    python twitter_thread_fetcher.py --url https://x.com/naval/status/1002109558058237953 --out ../dados/exemplos --nome thread_naval
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

SYNDICATION_ENDPOINT = "https://cdn.syndication.twimg.com/tweet-result"
URL_TWEET_RE = re.compile(r"(?:twitter\.com|x\.com)/[^/]+/status/(\d+)")
REQUEST_DELAY_SECONDS = 0.4
MAX_THREAD_HOPS = 200


class TweetNaoEncontrado(Exception):
    pass


@dataclass
class Tweet:
    id: str
    autor_nome: str
    autor_usuario: str
    texto: str
    criado_em: str
    curtidas: int
    respostas: int
    retweets: int
    in_reply_to_id: str | None
    in_reply_to_usuario: str | None

    @staticmethod
    def de_json(dado: dict) -> "Tweet":
        usuario = dado.get("user", {})
        return Tweet(
            id=dado["id_str"],
            autor_nome=usuario.get("name", ""),
            autor_usuario=usuario.get("screen_name", ""),
            texto=html.unescape(dado.get("text", "")),
            criado_em=dado.get("created_at", ""),
            curtidas=dado.get("favorite_count", 0),
            respostas=dado.get("conversation_count", 0),
            retweets=dado.get("retweet_count", 0),
            in_reply_to_id=dado.get("in_reply_to_status_id_str"),
            in_reply_to_usuario=dado.get("in_reply_to_screen_name"),
        )


def extrair_id_do_tweet(url_ou_id: str) -> str:
    """Aceita uma URL completa (twitter.com ou x.com) ou apenas o ID numerico."""
    if url_ou_id.isdigit():
        return url_ou_id
    m = URL_TWEET_RE.search(url_ou_id)
    if not m:
        raise ValueError(f"Nao foi possivel extrair o ID do tweet de: {url_ou_id}")
    return m.group(1)


def _token_sindicacao(tweet_id: str) -> str:
    """
    Formula publica usada por bibliotecas de embed (ex.: react-tweet) para gerar
    o parametro `token` do endpoint de sindicacao. O endpoint aceita tambem um
    token fixo na pratica, mas a formula real deixa a chamada mais resiliente
    a mudancas futuras de validacao.
    """
    import math

    valor = (int(tweet_id) / 1e15) * math.pi
    token = _base36(valor)
    return re.sub(r"(0+|\.)", "", token)


def _base36(valor: float) -> str:
    # Replica Number.toString(36) do JavaScript para floats, com casas decimais.
    inteiro = int(valor)
    fracao = valor - inteiro
    digitos = "0123456789abcdefghijklmnopqrstuvwxyz"

    parte_inteira = digitos[0] if inteiro == 0 else ""
    n = inteiro
    partes = []
    while n > 0:
        partes.append(digitos[n % 36])
        n //= 36
    parte_inteira = "".join(reversed(partes)) or "0"

    parte_fracionaria = ""
    f = fracao
    for _ in range(20):
        if f <= 0:
            break
        f *= 36
        digito = int(f)
        parte_fracionaria += digitos[digito]
        f -= digito

    return f"{parte_inteira}.{parte_fracionaria}" if parte_fracionaria else parte_inteira


def buscar_tweet(tweet_id: str, user_agent: str = "Mozilla/5.0 (compatible; CapturadorTopicosTwitter/1.0)") -> Tweet:
    token = _token_sindicacao(tweet_id)
    url = f"{SYNDICATION_ENDPOINT}?id={tweet_id}&token={token}"
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            dado = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise TweetNaoEncontrado(f"Tweet {tweet_id} nao encontrado (removido, privado ou invalido).") from e
        raise
    return Tweet.de_json(dado)


def montar_thread(url_ou_id: str) -> tuple[str, list[Tweet]]:
    """
    Retorna (classificacao, tweets_em_ordem_cronologica).
    classificacao: "tweet_unico" ou "thread".
    """
    tweet_id = extrair_id_do_tweet(url_ou_id)
    atual = buscar_tweet(tweet_id)
    autor = atual.autor_usuario
    cadeia = [atual]

    hops = 0
    while atual.in_reply_to_id and atual.in_reply_to_usuario == autor and hops < MAX_THREAD_HOPS:
        time.sleep(REQUEST_DELAY_SECONDS)
        atual = buscar_tweet(atual.in_reply_to_id)
        cadeia.append(atual)
        hops += 1

    cadeia.reverse()  # raiz primeiro, tweet informado por ultimo
    classificacao = "thread" if len(cadeia) > 1 else "tweet_unico"
    return classificacao, cadeia


def texto_markdown(classificacao: str, tweets: list[Tweet]) -> str:
    primeiro = tweets[0]
    linhas = [
        f"# Thread de @{primeiro.autor_usuario} ({primeiro.autor_nome})" if classificacao == "thread"
        else f"# Tweet de @{primeiro.autor_usuario} ({primeiro.autor_nome})",
        "",
        f"- Classificacao: **{classificacao}**",
        f"- Total de tweets: **{len(tweets)}**",
        f"- Primeiro tweet: {primeiro.criado_em}",
        f"- Link: https://x.com/{primeiro.autor_usuario}/status/{tweets[-1].id}",
        "",
        "---",
        "",
    ]
    for i, t in enumerate(tweets, start=1):
        prefixo = f"**{i}/{len(tweets)}**" if classificacao == "thread" else ""
        linhas.append(f"{prefixo} {t.texto}".strip())
        linhas.append(f"\n*{t.curtidas} curtidas - {t.respostas} respostas - {t.retweets} retweets*")
        linhas.append("")
    return "\n".join(linhas)


def salvar(classificacao: str, tweets: list[Tweet], destino: Path, nome: str) -> tuple[Path, Path]:
    destino.mkdir(parents=True, exist_ok=True)
    caminho_json = destino / f"{nome}.json"
    caminho_md = destino / f"{nome}.md"

    dados = {
        "classificacao": classificacao,
        "total_tweets": len(tweets),
        "autor_usuario": tweets[0].autor_usuario,
        "autor_nome": tweets[0].autor_nome,
        "tweets": [t.__dict__ for t in tweets],
    }
    caminho_json.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    caminho_md.write_text(texto_markdown(classificacao, tweets), encoding="utf-8")
    return caminho_json, caminho_md


def main() -> None:
    parser = argparse.ArgumentParser(description="Capturador de topicos (threads) do Twitter/X")
    parser.add_argument("--url", required=True, help="URL ou ID do tweet (qualquer tweet da thread)")
    parser.add_argument("--out", default="../dados/exemplos", help="Pasta de saida")
    parser.add_argument("--nome", default=None, help="Nome base dos arquivos gerados (sem extensao)")
    args = parser.parse_args()

    try:
        classificacao, tweets = montar_thread(args.url)
    except (TweetNaoEncontrado, ValueError) as e:
        print(f"Erro: {e}", file=sys.stderr)
        sys.exit(1)

    nome = args.nome or f"{classificacao}_{tweets[0].autor_usuario}_{tweets[-1].id}"
    destino = Path(args.out)
    caminho_json, caminho_md = salvar(classificacao, tweets, destino, nome)

    print(f"Classificacao: {classificacao}")
    print(f"Total de tweets capturados: {len(tweets)}")
    print(f"JSON: {caminho_json}")
    print(f"Markdown: {caminho_md}")


if __name__ == "__main__":
    main()
