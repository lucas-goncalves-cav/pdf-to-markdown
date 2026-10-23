# Decisões

Registro das decisões deste repositório que valem para quem integra com ele. A arquitetura
da automação do Esquematiza está no Google Drive (pasta "11 - Automação Inteligente e
Distribuição"): o doc 14 (Edital Analyzer e Content Diff) define o papel deste serviço, e o
doc 22 (Roadmap, adendo 19) pede o benchmark que motivou estas decisões. Se algo aqui
contradisser o Drive, vale o Drive.

## PM-001. NormalizedDocument v1 é o contrato entre sistemas; Markdown fica para pessoas

**Data:** 22/10/2026

O Edital Analyzer (no Automation Core) precisa apontar cada fato para a página e o trecho do
PDF oficial (doc 14, seções 4 e 13). Markdown perde página, posição, origem do texto e o que
não foi lido. Por isso o contrato máquina a máquina é o `NormalizedDocument v1`, JSON com
schema canônico em `schemas/normalized-document.v1.schema.json`, servido em
`POST /api/normalized-document` e pela linha de comando `python -m app.normalized`.

As rotas de Markdown continuam iguais e servem leitura humana.

## PM-002. Evidência por linha e por linha de tabela, em pontos, com origem no topo

Cada bloco traz `spans`: um por linha de texto, com página, caixa `[x0, top, x1, bottom]` e o
intervalo `[start, end)` do texto do bloco que veio daquela linha. Um trecho citado pelo
Analyzer (bloco e intervalo de caracteres) chega assim à linha exata, mesmo num parágrafo que
atravessa páginas. Tabelas trazem a página e a caixa de cada linha. Coordenadas em pontos do
PDF, origem no canto superior esquerdo, as mesmas do pdfplumber.

## PM-003. Identidade determinística; cache é do chamador

`document_id` deriva do SHA-256 do PDF, da versão do conversor e das opções que tiveram
efeito: limite de páginas não atingido e OCR num documento sem página escaneada não contam.
`normalized_content_hash` cobre páginas, blocos, tabelas e seções em JSON canônico e não
inclui `generated_at`. O mesmo PDF convertido do mesmo jeito tem o mesmo id e o mesmo hash.

O serviço não guarda estado. Quem chama (o adapter do Core) consulta o próprio armazenamento
pelo hash do PDF e pela versão do conversor antes de pedir conversão: parse once (doc 14,
seção 13). `CONVERTER_VERSION` sobe sempre que uma mudança puder alterar a saída do mesmo
PDF.

## PM-004. Quality gate documental dentro do contrato

Para cada página com camada de texto, o conversor conta letras e dígitos disponíveis e
emitidos (tabelas, linhas, rodapés removidos de propósito e carimbos girados contam como
lidos). A razão é a `coverage`. Abaixo de 0,98 a página recebe `LOW_TEXT_COVERAGE`, acima
de 1,02 recebe `TEXT_DUPLICATED`.

`quality.status`:

- `ok`: tudo lido da camada de texto com cobertura completa;
- `degraded`: OCR, truncamento, página não lida, cobertura baixa ou conteúdo girado;
- `unreadable`: nada saiu.

`quality.fallback_pages` lista as páginas que o consumidor deve ler do PDF se precisar delas
(doc 14: fallback só das páginas necessárias). A contagem por letras e dígitos, e não por
palavras, faz a junção de palavra hifenizada não parecer perda.

## PM-005. Texto girado

Carimbo de assinatura digital na margem (TCE-SP: "Documento assinado digitalmente...") é
texto girado. Agrupado por altura, virava uma letra por linha no meio dos parágrafos (22% de
texto espúrio no benchmark). Regra: texto girado fora de tabela em até 4 colunas é carimbo,
fica fora das linhas e gera `SIDEWAYS_TEXT_SKIPPED`. Com mais colunas é conteúdo (anexo em
paisagem desenhado girado): é mantido, sai fora de ordem e a página entra em
`fallback_pages` com `SIDEWAYS_CONTENT`. Ler conteúdo girado em ordem fica como gap.

## PM-006. Correções no conversor: refactor localizado, não substituição

O benchmark (`docs/benchmark-editais.md`) classificou o ativo como **REUSE WITH ADAPTER com
REFACTOR localizado** (doc 05, item 4): a extração é boa (perda abaixo de 0,25% em todos os
editais depois das correções), e os defeitos achados eram pontuais:

- letra de alínea (`a)`, `b)`) descartada em item de lista;
- item de lista quebrado em duas linhas virava item mais parágrafo solto;
- título numerado em caixa alta no corpo (`1. DAS DISPOSIÇÕES PRELIMINARES`) virava item de
  lista, sem seção;
- rodapé terminado em número de página sem marcador (`... | CONCURSO PÚBLICO 37`) não era
  removido e partia frases ao meio;
- texto dentro da área de uma tabela mas fora de qualquer célula (rodapé do SEI sob tabela
  que passa da página) se perdia.

Cada correção tem teste. Nenhuma reescreve o pipeline.

## PM-007. Este serviço não interpreta edital

Cargo, vagas, remuneração, banca, datas, etapas, requisitos e conteúdo programático são do
Edital Analyzer, no Automation Core (doc 03, seção 7.1; WORKSPACE.md). Aqui só existem
estrutura documental (página, bloco, título, parágrafo, lista, tabela, seção) e evidência.
Nenhuma regra deste repositório conhece concursos.

## PM-008. OCR continua reservado a página sem texto

Nenhum dos cinco editais oficiais do benchmark tinha página escaneada. O OCR foi medido com
páginas reais rasterizadas, comparando o texto do OCR com a camada de texto original do
mesmo PDF. Páginas lidas por OCR saem com `text_source: "ocr"`, confiança média e aviso
`OCR_USED`, e o documento fica `degraded`.

## PM-009. Sem LLM

Nada neste repositório chama modelo de linguagem. Conversão e quality gate são
determinísticos.
