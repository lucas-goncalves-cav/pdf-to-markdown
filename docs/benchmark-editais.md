# Benchmark com editais reais

Feito em 22/10/2026, antes de tornar o conversor a entrada do Edital Analyzer (doc 22 do Drive, adendo 19,
itens 4 e 5). A pergunta não é se o Markdown fica bonito: é se o resultado é confiável o bastante para um
analisador apontar cada fato para a página e o trecho do PDF oficial.

**Conclusão: REUSE WITH ADAPTER, com REFACTOR localizado** (doc 05, item 4). A extração já era boa; os
defeitos achados eram pontuais e foram corrigidos com teste (PM-006). O contrato máquina a máquina é o
NormalizedDocument v1 (PM-001).

## Editais

PDFs oficiais baixados do site da banca ou do órgão, com a lista aprovada antes do download. Nenhum fica
no repositório. Medir de novo: `python scripts/benchmark_editais.py <pdf>...`.

| Edital | Fonte | Páginas | Bytes | sha256 | Perfil |
|---|---|---|---|---|---|
| MPES, Edital 01/2026 (FGV) | conhecimento.fgv.br | 87 | 987.255 | `eef974be…3dbd` | grande, conteúdo programático por cargo e função, quadros com células mescladas |
| UFSCar TAE, abertura consolidado (Instituto Verbena) | sistemas.institutoverbena.ufg.br | 59 | 839.794 | `ea5303cf…8e2d` | muitas tabelas: cronograma, quadro de vagas de 15 colunas |
| SES-MG, Edital 01/2026 (IBGP) | saude.mg.gov.br | 44 | 366.952 | `12cebc2c…319d` | documento SEI (wkhtmltopdf), predominantemente textual |
| Guaíra/PR, Edital 001/2026 (Instituto Univida) | guaira.pr.gov.br | 45 | 870.838 | `d9b840c0…3489` | prefeitura, "Microsoft Print to PDF", tabelas sem bordas |
| TCE-SP, Edital EPCP 1/2026 | tce.sp.gov.br | 14 | 432.874 | `02986f04…dfe4b` | assinado digitalmente, com carimbo girado na margem de cada página |

O do TCE-SP é edital de concurso de trabalhos, não de cargos: entrou pelo carimbo de assinatura, não para
o Analyzer. Nenhum dos cinco tinha página escaneada; o OCR foi medido à parte (abaixo).

## Como medir

- **Perda e duplicação:** palavras do texto de cada página (hifenização de fim de linha desfeita, carimbo
  girado fora) contra as palavras dos blocos e tabelas. Cabeçalho e rodapé removidos de propósito não
  contam como perda.
- **Ordem:** semelhança entre a sequência de palavras da página e a do documento, só em páginas de prosa.
  Em tabela com célula de várias linhas a ordem por palavra não significa nada.
- **Estrutura:** seções (títulos), tabelas, e se cada bloco e cada linha de tabela sabem a página e a caixa.
- **Quality gate:** cobertura de letras e dígitos por página, status e avisos do próprio NormalizedDocument.

## Antes e depois

Mesma régua para o código da `main` (`8598547`) e o desta branch. O script versionado mede o estado atual
(coluna "depois") pelo NormalizedDocument; a `main` antiga não tem NormalizedDocument, então a coluna "antes"
foi medida com a mesma contagem de palavras aplicada aos blocos do pipeline dela (`extract_document`,
`strip_running_text`, `to_blocks`), sem spans. Nos dois lados, perda e duplicação são contadas no documento
inteiro.

| Edital | Perda antes | Perda depois | Duplicação antes | Duplicação depois | Títulos antes | Seções depois |
|---|---|---|---|---|---|---|
| MPES | 0,51% | 0,01% | 0,69% | 0,02% | 93 | 93 |
| UFSCar | 0,10% | 0,10% | 0,01% | 0,00% | 30 | 51 |
| SES-MG | 0,62% | 0,00% | 0,00% | 0,00% | 35 | 50 |
| Guaíra | 0,46% | 0,00% | 0,00% | 0,00% | 12 | 34 |
| TCE-SP | 0,00% | 0,24% | 26,82% | 0,00% | 20 | 20 |

- A perda que sobra na UFSCar é o cabeçalho repetido do quadro em cada página, descartado de propósito; a do
  TCE-SP são oito palavras de uma grade de notas.
- O "0,00%" de perda do TCE-SP antes escondia o problema: o carimbo girado entrava no texto como uma letra por
  linha (26,8% de texto espúrio, títulos como "1. DO OBJETO d0").
- No MPES o número de títulos é o mesmo, mas o conjunto mudou: entre outros, entram os títulos dos anexos III
  a VII, que antes ficavam colados ao parágrafo anterior.

Depois das correções, os cinco saem com `quality.status = ok` e cobertura 1,0. Ordem nas páginas de prosa:
mediana entre 0,97 e 0,999; a pior é a p. 37 do MPES, com 0,825.

Tempo de conversão, sem OCR: de 2 s (TCE-SP, 14 páginas) a 35 s (MPES, 87 páginas) nesta máquina, que estava
com outras cargas; isolado, o MPES levou 14 s. A busca de tabelas é a parte cara.

## Problemas achados

### Corrigidos nesta branch

| Problema | Onde apareceu | Efeito no Analyzer |
|---|---|---|
| Letra de alínea (`a)`, `b)`) descartada em item de lista | SES-MG, Guaíra, MPES | requisitos sem a alínea que o edital cita |
| Item de lista quebrado em duas linhas virava item mais parágrafo | SES-MG, MPES | requisito partido ao meio; tópico do programa partido no hífen ("Trans-" / "parência") |
| Título numerado em caixa alta no corpo virava item de lista | SES-MG, UFSCar, Guaíra | sem seções: "1. DAS DISPOSIÇÕES PRELIMINARES" não abria seção |
| Carimbo de assinatura girado entrava no texto | TCE-SP | 26,8% de texto espúrio |
| Rodapé terminado em número sem marcador não era removido | MPES ("... CONCURSO PÚBLICO 37") | frases e tópicos partidos pelo rodapé |
| Texto na área da tabela mas fora das células se perdia | SES-MG (tabela que passa da página engolia o rodapé) | páginas com cobertura 0,87 |
| Título que abre página colado ao parágrafo da página anterior | MPES (anexos III a VII) | o programa invadia os formulários dos anexos |
| Colunas deslocadas por células mescladas | MPES, UFSCar | "13" de AC lido sob "Escolaridade"; resolvido levando a caixa de cada célula ao contrato |

### Em aberto

- **Tabela sem bordas com célula de duas linhas** (cronograma de Guaíra): as linhas visuais são lidas em
  ordem de altura e a data de um evento gruda no seguinte. Sem bordas, o pdfplumber não acha a tabela.
- **Célula mesclada gera linhas de ruído** (UFSCar, quadro 2): linhas vazias e cabeçalho repetido entram
  como dados; quem lê precisa ignorar.
- **Tabela falsa em texto enquadrado** (MPES p. 44 e 63): duas colunas de texto lidas como tabela.
- **Conteúdo girado (anexo em paisagem desenhado de lado):** mantido, mas fora de ordem; a página sai em
  `fallback_pages` com `SIDEWAYS_CONTENT`.
- **Parágrafo que cruza a página sem título em caixa alta** continua um bloco só (MPES: "Função: Médico" no
  topo da p. 37 colado ao parágrafo da p. 36). O Analyzer trata; o conversor não tem como saber.

## OCR

Sem PDF escaneado no corpus, cinco páginas reais foram rasterizadas a 200 dpi, como um scanner faz, e lidas
com tesseract 5.5 (`por+eng`) no container do projeto. A camada de texto original das mesmas páginas é o
gabarito.

| Página | Recall | Precisão | Números recuperados | Confiança média |
|---|---|---|---|---|
| Guaíra p. 1, cronograma | 0,98 | 0,98 | 0,98 | 94,7 |
| Guaíra p. 4, quadro de cargos | 0,86 | 0,97 | 0,75 | 93,7 |
| Guaíra p. 16, conteúdo programático | 0,86 | 0,93 | 0,55 | 90,5 |
| MPES p. 3, quadro de vagas | 0,56 | 0,87 | 0,23 | 90,8 |
| MPES p. 38, conteúdo programático | 0,98 | 0,97 | 1,00 | 94,4 |

Prosa e programa saem bem. Tabela não: o OCR não reconstrói a tabela (as linhas da grade viraram imagem) e
os números de célula com grade, "0" e "1", ficam abaixo do corte de confiança. Cinco páginas levaram 42 s,
com a subida do container. Por isso a página lida por OCR sai marcada (`text_source: ocr`, `OCR_USED`,
documento `degraded`) e o Analyzer pede revisão de todo fato que venha dela.

## Evidência de volta ao PDF

Todo bloco traz os spans (página, caixa e intervalo do texto por linha) e toda linha de tabela traz a caixa
da linha e a de cada célula. No automation-core, o EditalManifest do MPES tem 2.214 evidências; para 2.212,
o texto que o PDF tem dentro da caixa citada contém a citação. As duas restantes estão numa das tabelas
falsas acima, em que o PDF tem uma palavra hifenizada.
