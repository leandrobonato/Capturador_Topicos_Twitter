# Documentação Técnica — Capturador de Tópicos do Twitter

## 1. Visão geral

O projeto tem duas implementações equivalentes da mesma lógica:

- **`workflow/Capturador_Topicos_Twitter.json`** — workflow n8n (produção/automação).
- **`scripts/twitter_thread_fetcher.py`** — script Python (referência, testes locais, agendamento via cron).

Ambos implementam o mesmo algoritmo de 4 passos: extrair ID → buscar tweet → caminhar a cadeia de respostas → classificar e formatar.

## 2. Fonte de dados: endpoint de sindicação do Twitter/X

`https://cdn.syndication.twimg.com/tweet-result?id={id}&token={token}`

Este é o endpoint público que o próprio Twitter/X usa para gerar os "cards" de embed de tweets em sites de terceiros (o widget `<blockquote class="twitter-tweet">`). Não exige autenticação, cookie de sessão nem chave de API — é a mesma técnica usada pela biblioteca open source [`react-tweet`](https://github.com/vercel/react-tweet) (Vercel, MIT license) para renderizar tweets sem depender da API paga.

### Parâmetro `token`

O endpoint aceita, na prática, qualquer valor não vazio, mas a fórmula "correta" (usada pelo `react-tweet` e replicada aqui para robustez caso a validação fique mais rígida no futuro) é:

```js
token = ((Number(tweetId) / 1e15) * Math.PI).toString(36).replace(/(0+|\.)/g, '')
```

Implementada em JavaScript no nó "Montar Thread" do workflow, e em Python (`_token_sindicacao` / `_base36`) no script de referência — o Python precisou de uma reimplementação manual de `Number.toString(36)` para floats, já que a stdlib não tem equivalente direto.

### Campos relevantes da resposta

```json
{
  "id_str": "...",
  "text": "...",
  "created_at": "...",
  "favorite_count": 0,
  "conversation_count": 0,
  "user": { "name": "...", "screen_name": "..." },
  "in_reply_to_status_id_str": "... ou ausente",
  "in_reply_to_screen_name": "... ou ausente",
  "parent": { "...um nível do tweet-pai, mesmos campos..." }
}
```

O campo `parent` traz **apenas um nível** acima (não a cadeia inteira) — por isso o algoritmo precisa de uma chamada HTTP por tweet da thread, não uma chamada só.

## 3. Algoritmo de reconstrução da thread

```
tweet_atual = buscar(id_informado)
autor = tweet_atual.autor_usuario
cadeia = [tweet_atual]

enquanto tweet_atual.in_reply_to_id existe
      E tweet_atual.in_reply_to_usuario == autor
      E hops < 200:
    tweet_atual = buscar(tweet_atual.in_reply_to_id)
    cadeia.append(tweet_atual)

inverter cadeia  # raiz primeiro, tweet informado por último
classificacao = "thread" se len(cadeia) > 1 senão "tweet_unico"
```

Este é o mesmo princípio usado por serviços conhecidos de "unroll" de threads (ex.: Thread Reader App): a reconstrução caminha **para trás**, do tweet informado até a raiz, seguindo apenas respostas do próprio autor. É o formato de praticamente toda thread real do Twitter/X (o autor responde à sua própria sequência de tweets).

**Limite de segurança:** `MAX_HOPS = 200` evita loop infinito em caso de dado inconsistente da API.

### Por que não caminha "para frente"?

O Twitter/X não expõe publicamente "qual é o próximo tweet de uma thread" — essa informação só está disponível via:

1. **API de busca paga (v2, Basic tier ou superior)**, filtrando por `conversation_id:{id} from:{autor}` e ordenando por ID — funciona apenas para os últimos 7 dias na busca "recent", ou requer acesso "full-archive" (Academic/Enterprise) para threads antigas.
2. **A própria interface web do X**, que resolve isso client-side usando a API interna GraphQL autenticada por sessão de guest — mecanismo mais frágil e mais próximo do limite dos Termos de Serviço, por isso não foi usado aqui.

**Implicação prática:** para capturar a thread **inteira**, informe a URL do **último** tweet da sequência (o que o usuário normalmente tem em mãos ao salvar/compartilhar uma thread que acabou de ler). Se for informado um tweet do meio, apenas os tweets **até ele** (não os posteriores) são recuperados.

## 4. Alternativa de produção: API oficial do X (v2)

Para uso comercial recorrente ou alto volume, substitua a busca por sindicação por chamadas à API v2 oficial:

1. `GET /2/tweets/:id?tweet.fields=conversation_id,author_id` — obtém o `conversation_id` da thread.
2. `GET /2/tweets/search/recent?query=conversation_id:{id} from:{author_id}&tweet.fields=in_reply_to_user_id,created_at` — retorna os tweets da conversa (últimos 7 dias no plano Basic; full-archive requer Academic/Enterprise).
3. Ordenar por `id` (crescente) e filtrar apenas a cadeia direta de auto-respostas (descartar respostas de terceiros).

Essa é a via **sancionada oficialmente** pelo X, sem depender de um endpoint não documentado — trade-off: exige Bearer Token pago (a partir do plano Basic, ~US$ 200/mês) para busca de `conversation_id`.

No n8n, essa alternativa substitui o nó "Montar Thread" por dois nós **HTTP Request** com credencial **HTTP Bearer Auth** (Bearer Token do X), mantendo o restante do workflow (formatação, resposta, IA, planilha) inalterado.

## 5. Testes realizados

Testado em 2026-07-30, contra tweets públicos reais:

| Caso | Entrada | Resultado |
|---|---|---|
| Thread completa | Último tweet da thread "How to Get Rich" de `@naval` (`.../status/1002109558058237953`) | `classificacao: thread`, 40 tweets recuperados em ordem cronológica correta, raiz = `.../status/1002103360646823936` |
| Tweet único | Primeiro tweet da história do Twitter, `@jack` (`.../status/20`) | `classificacao: tweet_unico`, 1 tweet, sem `in_reply_to` |

Saídas reais salvas em `dados/exemplos/` (JSON + Markdown).

Validação adicional: confirmado por inspeção manual (`curl` direto ao endpoint de sindicação) que:
- O campo `parent` traz só 1 nível de profundidade (não a cadeia inteira).
- `in_reply_to_screen_name` permite distinguir auto-resposta (thread) de resposta de terceiro (não-thread), essencial para a condição de parada do algoritmo.

## 6. Estrutura de arquivos

```
Capturador_Topicos_Twitter/
├── README.md                                  # Portfólio (comercial)
├── docs/
│   └── DOCUMENTACAO_TECNICA.md                # Este arquivo
├── workflow/
│   └── Capturador_Topicos_Twitter.json        # Workflow n8n (importável)
├── scripts/
│   └── twitter_thread_fetcher.py              # Implementação de referência em Python
├── dados/
│   └── exemplos/                              # Capturas reais geradas nos testes
│       ├── thread_naval_como_enriquecer.json
│       ├── thread_naval_como_enriquecer.md
│       ├── tweet_unico_jack.json
│       └── tweet_unico_jack.md
└── .gitignore
```

## 7. Limitações e riscos conhecidos

- **Dependência de endpoint não documentado oficialmente**: o Twitter/X pode alterar ou restringir o endpoint de sindicação a qualquer momento, sem aviso. Mitigação: seção 4 documenta a via oficial (API paga) como substituição direta.
- **Rate limiting**: não há limite documentado publicamente para o endpoint de sindicação, mas o script de referência inclui um atraso de 0,4s entre chamadas (`REQUEST_DELAY_SECONDS`) por precaução; o workflow n8n não tem atraso embutido — adicione um nó **Wait** entre iterações se for processar threads muito longas em lote.
- **Apenas tweets de texto/metadados básicos**: mídia (imagens, vídeo), enquetes e citações não são tratadas especificamente — o campo `texto` cobre apenas o texto do tweet.
