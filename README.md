# GumbInvest

🇧🇷 Português · [🇺🇸 English](README.en.md)

**Gerenciador de carteira de investimentos para o investidor brasileiro, rodando
no seu próprio computador.** Importe os arquivos que suas corretoras já
fornecem — o CSV de "Movimentação" da B3, os extratos em PDF da Avenue e da
Nomad, as exportações da Binance — e o GumbInvest reconstrói cada posição com
preço médio, avalia tudo com cotações ao vivo, rende sua renda fixa pelas
séries oficiais de CDI/IPCA e apresenta o resultado num dashboard escuro e
moderno. Com um analista de IA opcional por cima.

Seus dados nunca saem da sua máquina: tudo roda no seu computador, com Docker
ou como um aplicativo desktop comum para Windows, sem nenhuma infraestrutura.

⬇️⬇️ [**Baixar para Windows ou macOS**](https://github.com/gustavobonassa/gumbinvest/releases/latest) ⬇️⬇️

![Dashboard](docs/screenshots/dashboard.png)

```bash
cp .env.example .env
docker compose up -d
# abra http://localhost:3000
```

Coloque seus arquivos em `./data/` antes da primeira inicialização — `data/b3/`
para o CSV da B3, `data/avenue/` e `data/nomad/` para os extratos em PDF,
`data/binance/` para as exportações da corretora cripto — e tudo é importado
automaticamente. O nome dos arquivos não importa: o tipo de cada um é
identificado pelo conteúdo. Você também pode enviar arquivos depois, pela
página **Importar**.

> Todas as capturas de tela deste README mostram uma **carteira de demonstração
> fictícia** — tickers reais e cotações de mercado ao vivo, com quantidades e
> datas inventadas.

---

## Sumário

- [Capturas de tela](#capturas-de-tela)
- [Funcionalidades](#funcionalidades)
- [Início rápido](#início-rápido)
  - [Aplicativo desktop (Windows, sem Docker)](#aplicativo-desktop-windows-sem-docker)
- [Documentação](#documentação)
- [Arquitetura](#arquitetura)
- [Desenvolvimento](#desenvolvimento)
- [Testes](#testes)
- [Privacidade](#privacidade)
- [Como contribuir](#como-contribuir)
- [Licença](#licença)

---

## Capturas de tela

| | |
|---|---|
| **Proventos** — renda por mês, calendário, matriz e maiores pagadores | **Rentabilidade** — retorno acumulado vs. CDI e Ibovespa |
| ![Proventos](docs/screenshots/dividends.png) | ![Rentabilidade](docs/screenshots/reports.png) |
| **Ativos** — todas as posições, ordenáveis, por classe de ativo | **Transações** — o histórico completo, com filtros e exportação em CSV |
| ![Ativos](docs/screenshots/assets.png) | ![Transações](docs/screenshots/transactions.png) |

**Analista IA** — um chat de IA que enxerga sua carteira e pesquisa na web,
aberto ao lado da página:

![Analista IA](docs/screenshots/ai-analyst.png)

E a interface inteira é responsiva — dá para usar pelo celular, no sofá (o
aplicativo desktop até mostra um QR code para abrir no celular pela rede local):

| Dashboard | Proventos |
|---|---|
| ![Dashboard no celular](docs/screenshots/mobile-dashboard.png) | ![Proventos no celular](docs/screenshots/mobile-dividends.png) |

---

## Funcionalidades

**Importação**
- Lê o CSV de "Movimentação" da B3 do jeito que ele vem: números no formato
  brasileiro (`R$ 1.234,56`), datas `dd/mm/aaaa`, `-` para "não se aplica",
  codificações UTF-8/BOM/latin-1, separadores `,` ou `;`.
- Entende mais de 40 tipos de movimentação — compras e vendas, dividendos, JCP,
  rendimentos, juros, amortizações, desdobramentos, grupamentos, bonificações,
  subscrições, incorporações, frações, transferências de custódia, vencimentos
  de renda fixa, taxas e impostos.
- **Idempotente**: enviar o mesmo arquivo de novo não acrescenta nada. Arquivos
  mensais somam só o que é novo, e movimentações realmente repetidas (mesmo
  ativo, dia, preço e quantidade) continuam sendo mantidas.
- Movimentações desconhecidas são importadas para fins de auditoria, sinalizadas
  no log de importação e nunca aplicadas silenciosamente a uma posição.
- Lê os **extratos mensais em PDF** da Avenue e da Nomad — todos os layouts que
  as duas corretoras usaram desde 2020 — e valida cada um contra os próprios
  totais impressos, com um **mapa de cobertura** mostrando quais meses você tem
  e quais posições deixaram de bater com os números da corretora.
- Lê as **exportações spot da Binance** (histórico de transações, trades e
  ordens). Uma negociação numa exchange é uma troca, então os dois lados são
  lançados: comprar Ether com Tether gasta o Tether.
- **Coleta automática (Automações)** — uma rotina agendada entra na Área do
  Investidor da B3 com seu CPF, exporta a movimentação e importa, sem nenhum
  download manual. Se a B3 pedir um código de verificação, o app pede na tela
  (Importar → Automações); tudo o que é coletado passa pelo mesmo importador
  idempotente de um envio manual.
- **Binance pela API** — cole uma chave de API *somente leitura* em Importar →
  Automações e o app coleta trades spot, Convert, depósitos e saques de cripto,
  compras com cartão/PIX, Simple Earn e distribuições no próprio agendamento;
  depois confere cada moeda contra os saldos informados pela Binance e lista as
  que divergem. A primeira execução traz o histórico inteiro; uma exportação
  enviada antes (ou depois) é conciliada com ele, nunca lançada em dobro.

**Carteira**
- Preço médio ponderado, a convenção brasileira.
- **Multimoeda**: ativos nos EUA ficam em dólar e aparecem em dólar na própria
  página, convertidos para reais sempre que é preciso um total único — o custo
  pela PTAX de cada compra, o valor de mercado pela de hoje.
- Resultado realizado e não realizado, proventos por tipo, capital devolvido,
  custo de aquisição, alocação, variação do dia, fluxo de caixa.
- Tratamento correto de vendas parciais, compras múltiplas,
  desdobramentos/grupamentos, bonificações, subscrições, incorporações,
  restituição de capital e transferências entre corretoras.
- Avisos de qualidade dos dados sempre que a própria exportação for
  inconsistente.

**Dashboard e análises**
- Patrimônio vs. capital investido ao longo do tempo — clique e arraste para
  medir qualquer trecho da série —, gráfico de rosca de alocação (por ativo,
  classe, corretora ou moeda), renda mensal, aportes e retornos, resultado por
  ativo, maiores posições, maiores altas e baixas do dia.
- **Rentabilidade**: retorno acumulado a partir de zero, comparado com CDI e
  Ibovespa, por classe ou da carteira inteira; consistência mensal,
  melhores/piores meses, ranking de desempenho por ativo.
- Páginas de ativo com histórico de preço, linha de referência do preço médio,
  fundamentos, histórico de proventos por ação, proventos anunciados e o
  histórico completo de movimentações.
- Transações com busca, filtros, ordenação, paginação e exportação em CSV.
- Busca global (`Ctrl/⌘ + K`) por tickers, nomes de empresas, movimentações e
  datas.

**Proventos**
- Uma página só para a renda: dividendos, JCP, rendimentos de fundos e juros de
  renda fixa, agrupados por **mês, trimestre ou ano** e empilhados por tipo de
  pagamento ou classe de ativo.
- Um **calendário de proventos**: o que caiu em cada dia, mais os pagamentos já
  anunciados na B3 e ainda não creditados, estimados pela sua posição atual.
- Ranking de pagadores, **yield on cost** por ativo e uma matriz mês × ano com
  todos os pagamentos desde o primeiro, sombreada pelo valor.

**IRPF**
- Uma página por ano-calendário com os blocos que a declaração pede: **Bens e
  Direitos** pelo custo de aquisição em 31/12, com o ano anterior ao lado,
  agrupados por grupo/código e com a discriminação já escrita para copiar;
  **rendimentos isentos**, **tributação exclusiva** e rendimentos do exterior
  separados, cada um identificado por fonte pagadora e CNPJ; e as alienações
  mês a mês, contra a isenção que cada classe de fato tem.
- O que ela *não consegue* responder vem primeiro, acima de qualquer número que
  seria afetado: uma fonte pagadora sem CNPJ cadastrado, cotas de custo
  desconhecido, um vencimento que o extrato registrou sem o valor, um saldo que
  é uma projeção pelo CDI e não um extrato bancário. Nada é preenchido com um
  número plausível.
- Uma planilha para transcrever, nunca uma declaração enviada.

**Impostos (IR mensal sobre vendas)**
- A apuração mês a mês por trás do DARF (código 6015): ações, units, ETFs e
  BDRs a 15 % com a isenção de R$ 20.000 mensais na venda de ações, FIIs a
  20 % e day trade a 20 % a partir do resultado que você informa pela nota da
  corretora — a exportação da B3 não marca day trades, então pares de compra e
  venda no mesmo dia são sinalizados em vez de adivinhados.
- Prejuízos são compensados em bases separadas (operações comuns, FIIs, day
  trade), o IRRF de 0,005 % é estimado e deduzido dentro do ano, um DARF abaixo
  de R$ 10 passa para o mês seguinte e cada mês mostra o vencimento (último dia
  útil do mês seguinte, considerando feriados bancários). Marque um DARF como
  pago e ele deixa de constar como devido.
- Ativos no exterior (anuais, pela Lei 14.754/2023), cripto e renda fixa
  aparecem à parte, nunca somados ao valor mensal. Uma planilha para conferir,
  como a página de IRPF — e o analista de IA também a lê.

**Renda fixa e Tesouro Direto**
- Posições em CDB/LCI/LCA/RDB **rendem pelo indexador**, não ficam congeladas
  no custo: as séries de CDI, Selic e IPCA do Banco Central (gratuitas, sem
  chave) determinam a avaliação.
- Condições por título: % do CDI/Selic, CDI + spread, prefixado ou IPCA +
  spread; os fluxos de caixa de um título resgatado são resolvidos de trás para
  frente para revelar a taxa que ele realmente pagou.
- O Tesouro Direto é marcado a mercado pelo arquivo diário de preços do próprio
  Tesouro, incluindo a taxa contratada em cada compra.
- Contas remuneradas, com depósitos e saques rendendo do mesmo jeito.

**IA (opcional, com a sua própria chave — ou sua assinatura do Claude)**
- **Analista IA** — converse sobre sua carteira ou sobre um ativo específico. O
  modelo consulta seus dados por ferramentas somente leitura — posições,
  histórico de transações, proventos recebidos, alocação, retornos mensais e
  proventos anunciados —, então consegue responder "quanto de JCP a ITSA4 me
  pagou em 2024?" com os mesmos números das telas, e pode pesquisar na web.
  Respostas em streaming, com tabelas e listas, e conversas salvas numa página
  própria. As ferramentas funcionam com todos os provedores, inclusive a
  assinatura do Claude (servidas ao Claude Code por um endpoint MCP local, um
  por resposta).
- **Carteira IA** — carteiras virtuais geridas de ponta a ponta por um modelo
  de IA, com R$ 10.000 virtuais por categoria: ele faz a triagem dos
  candidatos, justifica cada escolha e revisa a carteira ao longo do tempo.
  Nada toca na sua carteira real.
- Funciona com **Anthropic (Claude), OpenAI (GPT), Google (Gemini), xAI (Grok)
  e Groq** — escolha o provedor e o modelo de cada funcionalidade em
  Configurações.
- **Plano Anthropic** — já paga o Claude Pro/Max? Conecte a conta com um
  clique e rode todas as funcionalidades de IA na sua assinatura, em vez de uma
  chave de API cobrada por uso. Só no desktop, e exige o
  [Claude Code](https://claude.com/download) instalado na mesma máquina (é ele
  que guarda o login); o uso conta nos limites do seu plano, não em créditos de
  API.

**Screener e ferramentas**
- **Universo de ativos** — filtre *todos* os papéis listados na B3, não só os
  que você tem, por fundamentos calculados localmente; veja onde seus ativos
  ficam dentro do mercado inteiro ("como me comparo", por percentil).
- **Comparador** — desempenho de preço indexado e fundamentos lado a lado para
  quaisquer tickers, na carteira ou não; a comparação fica na URL, então dá
  para compartilhar.
- **Carteiras públicas** — as carteiras 13F de investidores famosos, direto do
  SEC EDGAR.
- **Calculadora de juros compostos** — já começa com o seu patrimônio real e o
  seu aporte mensal médio, então a primeira projeção é "minha carteira, se eu
  continuar assim".

**Operação**
- Cotações ao vivo com provedores intercambiáveis; atualização automática em
  segundo plano.
- Fechamentos diários históricos, para que o gráfico de evolução mostre o valor
  de mercado real, não o custo.
- Snapshots noturnos e backups automáticos semanais com rotação, além de uma
  exportação `.gumbinvest` em arquivo único que leva o histórico inteiro de uma
  instalação para outra.
- Backup na nuvem no seu próprio Google Drive ou Dropbox — semanal e sob
  demanda, opcionalmente criptografado com uma senha — com restauração a partir
  da nuvem numa instalação nova (Configurações → Backup).
- Lista de acompanhamento, anotações por ativo, preços manuais, log de
  auditoria.
- **Modo privacidade** — o olho na barra superior esconde todos os valores e
  quantidades atrás de `••••`, em todas as páginas e nos eixos dos gráficos,
  para quem estiver atrás de você. Percentuais, datas, tickers e cotações
  continuam visíveis, então a tela ainda vale a pena. A escolha fica salva e
  sobrevive a um recarregamento.
- **Tema escuro ou claro** — Configurações → Geral. O escuro é o padrão e a
  casa do design; o claro tem sua própria escala de cores (não é uma
  inversão), fica salvo junto com o resto das suas preferências e é aplicado
  antes da primeira renderização, sem piscar ao carregar. No aplicativo desktop,
  os botões nativos da janela acompanham o tema.
- **Automações** — coletores (hoje a B3 pelo navegador e a Binance pela API;
  outras corretoras e fontes de dados conforme forem adicionadas) que, depois
  de ativado o agendamento, rodam nos dias da semana e no horário que você
  escolher para cada um, guardam o histórico completo de execuções e podem ser
  disparados ou cancelados em Importar → Automações. Um horário perdido porque
  o computador ou o aplicativo estava desligado roda assim que ele volta.

---

## Início rápido

Requisitos: Docker e Docker Compose. Mais nada.

```bash
git clone https://github.com/gustavobonassa/gumbinvest.git && cd gumbinvest
cp .env.example .env

# opcional: importa seu histórico automaticamente na primeira inicialização —
# uma pasta por fonte, qualquer nome de arquivo (o importador identifica o
# tipo de cada arquivo pelo conteúdo)
cp /caminho/para/exportacao-b3.csv    ./data/b3/       # CSV(s) de "Movimentação" da B3
cp /caminho/para/extratos-avenue/*    ./data/avenue/   # extratos em PDF da Avenue
cp /caminho/para/extratos-nomad/*     ./data/nomad/    # extratos em PDF da Nomad
cp /caminho/para/exportacoes-binance/* ./data/binance/ # exportações CSV da Binance

docker compose up -d
docker compose logs -f backend      # acompanhe a importação
```

| Serviço | URL |
|---|---|
| Dashboard | http://localhost:3000 |
| API | http://localhost:8000/api |
| Documentação da API (Swagger) | http://localhost:8000/api/docs |
| Postgres | `localhost:5433` |

A primeira execução aplica as migrations, cria a carteira padrão, baixa o
histórico da cotação USD/BRL e importa todos os CSVs *e* extratos em PDF
encontrados em `AUTO_IMPORT_DIR` (`./data` por padrão, com busca recursiva). As
pastas por fonte são só uma convenção para manter o arquivo organizado —
arquivos na raiz também funcionam, os nomes são livres e rodar de novo nunca
duplica nada.

Depois, pela interface: **Configurações → Baixar histórico de preços** para
buscar os fechamentos diários (leva alguns minutos), o que transforma o gráfico
de evolução em valor de mercado de verdade.

Para parar: `docker compose down`. Para apagar tudo, inclusive o banco de
dados: `docker compose down -v`.

### Aplicativo desktop (Windows, sem Docker)

Para quem só quer instalar e usar, existe uma versão empacotada para Windows:
uma janela Electron (com barra de título escura, no estilo do app) envolvendo
o aplicativo inteiro rodando localmente — SQLite no lugar do Postgres, um
agendador dentro do próprio processo no lugar do Celery, o dashboard servido
pela própria API. Sem Docker, sem infraestrutura. A janela é só um
visualizador: o mesmo servidor local continua acessível por qualquer navegador
da máquina e pelo celular.

**[⬇ Baixar o instalador mais recente](https://github.com/gustavobonassa/gumbinvest/releases/latest)**
— `GumbInvest-Setup-<versão>.exe` para Windows, `GumbInvest-<versão>-arm64.dmg`
para macOS (Apple Silicon). Cada versão é compilada automaticamente nos
servidores do GitHub a partir do código-fonte marcado com a tag
([`.github/workflows/release.yml`](.github/workflows/release.yml)).

Os instaladores não são notarizados pela Apple nem assinados para o Windows,
então o sistema reclama uma vez na primeira execução:

- **Windows** — SmartScreen: "Mais informações" → "Executar assim mesmo".
- **macOS** — depois de arrastar o app para Aplicativos, remova a quarentena do
  download uma vez: `xattr -d com.apple.quarantine /Applications/GumbInvest.app`
  (ou Ajustes do Sistema → Privacidade e Segurança → "Abrir Mesmo Assim" depois
  da primeira tentativa bloqueada).

A versão para macOS compartilha todo o código, mas é mais nova e menos testada
na prática que a do Windows; os dados ficam em
`~/Library/Application Support/GumbInvest`.

Ou compile você mesmo:

```powershell
# precisa de Node e Python 3.11+
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# saída: desktop-shell\out\GumbInvest-Setup-<versão>.exe
```

O que o aplicativo instalado faz:

- Abre sua própria janela, com a barra de título desenhada no tema do app,
  escuro ou claro (Window Controls Overlay do Electron); fechar a janela
  encerra o app. Um ícone na bandeja do sistema reúne as ações secundárias:
  **Abrir** (traz a janela de volta), **Abrir no celular** (QR code com o
  endereço na rede local — permita o GumbInvest em *redes privadas* quando o
  Firewall do Windows perguntar, senão o celular não conecta), **Abrir pasta de
  dados**, **Sair**. `http://localhost:8873` também funciona em qualquer
  navegador da máquina, se preferir.
- Os dados ficam em `%LOCALAPPDATA%\GumbInvest` (banco de dados, backups, logs
  e uma pasta `auto-import` com a mesma estrutura `b3/`, `avenue/`, `nomad/`,
  `binance/` do `./data` no Docker). Desinstalar mantém os dados; reinstalar
  encontra tudo de novo.
- As tarefas agendadas (atualização de cotações, sincronização de câmbio,
  índices e Tesouro, snapshot noturno e backup) rodam dentro do app — no mesmo
  horário do Celery beat.
- **Atualiza sozinho**: Configurações → Geral mostra a versão instalada e
  procura uma mais nova no GitHub Releases (também uma vez, discretamente, uns
  15 s depois de abrir). Baixar e reiniciar na versão nova são dois botões
  explícitos — nada é baixado ou instalado sem você saber, e seus dados, chaves
  e backups não são tocados na troca. Só no Windows: um app de macOS sem
  assinatura não consegue se atualizar sozinho, então essa versão aponta para
  a página de releases.
- `DATABASE_URL` continua tendo prioridade se estiver definida, então uma
  instalação desktop pode usar um Postgres existente.

Publicar uma nova versão (para mantenedores): aumente `version` em
`desktop-shell/package.json` e rode `git tag vX.Y.Z && git push origin vX.Y.Z`.
O workflow compila os dois instaladores e os anexa — junto com o
`latest.yml`, que é o feed consultado pelos apps instalados — ao GitHub
Release. Os apps já instalados veem a atualização na próxima verificação.

Desktop e Docker são instâncias separadas, com dados separados — não existe
sincronização ao vivo entre um SQLite do desktop e um Postgres de servidor.
Para levar o histórico de um para o outro uma vez: **Configurações → Backup e
migração → Exportar banco de dados** na origem e arraste o arquivo
`.gumbinvest` para a página **Importar** do destino. O arquivo é um clone
completo (todas as tabelas, com os IDs preservados, em JSON compactado com
gzip); importar substitui tudo e só é aceito por uma instância que ainda não
tem transações, então dois históricos nunca são mesclados sem você perceber.
As duas instalações precisam estar na mesma versão do schema.

Durante o desenvolvimento, rode exatamente o código do desktop sem empacotar:

```powershell
cd backend
pip install -r requirements-desktop.txt
python -m app.desktop          # o servidor: SQLite + agendador + SPA na :8873
# opcional, em outro terminal — a janela Electron em volta dele:
cd ..\desktop-shell && npm install && npm start
```

---

## Documentação

Os detalhes ficam em `docs/`, um guia por assunto (por enquanto, em inglês):

| Guia | O que cobre |
|---|---|
| [Importando seus dados](docs/importing.md) | O pipeline do CSV da B3, os extratos em PDF da Avenue/Nomad, as exportações da Binance, a deduplicação e as ressalvas conhecidas de cada fonte |
| [Como os números são calculados](docs/calculations.md) | O motor de replay e suas regras (desdobramentos, `Atualização`, frações…), multimoeda, eventos corporativos, avaliação de renda fixa e Tesouro Direto |
| [IA, screener e ferramentas](docs/features.md) | Analista IA, Carteira IA, o screener do universo de ativos, Comparador, carteiras públicas 13F, a calculadora |
| [Referência](docs/reference.md) | Provedores de dados de mercado, variáveis de ambiente, endpoints da API, testes, backups |
| [Arquitetura em detalhe](docs/ARCHITECTURE.md) | O *porquê* de cada decisão de design — leia antes de mexer no importador ou no motor |

---

## Arquitetura

```
                    ┌──────────────┐
  navegador ──────► │   frontend   │  React + TS + Tailwind + Recharts
                    │    nginx     │  serve a SPA, faz proxy de /api (mesma origem)
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐        ┌──────────────┐
                    │   backend    │◄──────►│  PostgreSQL  │
                    │   FastAPI    │        └──────────────┘
                    └──────┬───────┘
                           │                ┌──────────────┐
                    ┌──────▼───────┐◄──────►│    Redis     │
                    │ worker+beat  │        └──────────────┘
                    │    Celery    │  cotações · histórico · snapshots · backups
                    └──────────────┘
```

A versão desktop troca as pontas e mantém o núcleo: SQLite no lugar do
Postgres, um APScheduler dentro do processo no lugar do Celery, a SPA servida
pelo próprio FastAPI — o mesmo código da aplicação atrás de uma única janela
(`backend/app/desktop/`, `desktop-shell/`).

Os princípios sobre os quais todo o código é construído: dinheiro é `Numeric`,
nunca float; transações só são acrescentadas, nunca alteradas, e as posições
são sempre derivadas reprocessando-as; importações são idempotentes; o
classificador é uma tabela, não lógica; ambiguidades são expostas, nunca
adivinhadas em silêncio. O raciocínio por trás de cada um está em
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Desenvolvimento

Suba a stack com Docker e aponte o servidor de desenvolvimento do Vite para
ela:

```bash
docker compose up -d db redis backend

cd frontend
npm install
npm run dev          # http://localhost:5173, faz proxy de /api para a :8000
```

Backend sem Docker:

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate    # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://gumbinvest:gumbinvest@localhost:5433/gumbinvest
alembic upgrade head
uvicorn app.main:app --reload
```

Depois de alterar um model:

```bash
docker compose exec backend alembic revision --autogenerate -m "what changed"
docker compose exec backend alembic upgrade head
```

As migrations precisam continuar compatíveis com SQLite (a versão desktop roda
em SQLite) — `tests/test_migrations_sqlite.py` garante isso.

**Publicando uma versão:** envie uma tag de versão e o GitHub Actions compila
o instalador do Windows e o publica na página de releases automaticamente:

```bash
git tag v1.0.0
git push origin v1.0.0
```

---

## Testes

```bash
cd backend && python -m pytest -q      # SQLite, sem precisar de infraestrutura
cd frontend && npm run check          # typecheck, ESLint, checagem do Prettier, Vitest
```

Todo push e pull request roda os dois no GitHub Actions
(`.github/workflows/ci.yml`) antes de qualquer coisa chegar a uma tag de
release.

A suíte cobre o dialeto do CSV, todos os rótulos de movimentação, o conjunto
completo de regras de cálculo, a deduplicação, a conciliação de extratos e de
cripto, a ingestão do universo de ativos e a camada HTTP. Os detalhes —
incluindo como os parsers são validados contra os totais impressos nos próprios
documentos — estão em [docs/reference.md](docs/reference.md#tests).

---

## Privacidade

- **Tudo é local.** Suas transações, extratos e saldos ficam no seu próprio
  Postgres/SQLite. Não há telemetria, nem conta, nem nuvem.
- **As chamadas externas se limitam a dados públicos de mercado**: provedores
  de cotação (Yahoo/brapi), Banco Central (PTAX, CDI/Selic/IPCA), Tesouro
  Transparente, arquivos públicos da B3/CVM e SEC EDGAR. Nenhum deles recebe
  sua carteira.
- **As funcionalidades de IA são opcionais** e só funcionam com a sua própria
  chave de API. Quando você as usa, o contexto relevante da carteira é enviado
  ao provedor que *você* configurou — e a mais nenhum lugar. As chaves ficam no
  banco de dados local e são removidas de toda exportação e todo backup.
- Se for fazer um fork ou contribuir: `data/`, `backups/` e `.env` estão no
  `.gitignore` por um motivo — nunca faça commit de histórico financeiro real e
  use dados fictícios nos testes.

---

## Como contribuir

Issues e pull requests são bem-vindos. Antes de mexer no importador, no motor
ou na lógica de deduplicação, leia [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
— ele explica o *porquê* de cada decisão de design. A versão curta:

- dinheiro é `Decimal`, nunca float;
- transações só são acrescentadas, as posições são sempre derivadas;
- as importações precisam continuar idempotentes;
- o classificador é uma tabela, não lógica — novas descrições de corretora são
  dados;
- ambiguidades são expostas, nunca adivinhadas em silêncio;
- os parsers de extrato precisam ser validados contra os totais impressos nos
  próprios documentos.

Novas descrições de movimentação, layouts de extrato e formatos de exportação
de exchanges são as contribuições mais valiosas — e normalmente as menores: uma
linha no classificador mais um teste.

Código, comentários e mensagens de commit ficam em inglês.

```bash
git config core.hooksPath .githooks   # uma vez por clone: as checagens da mensagem de commit
cd backend && python -m pytest -q     # precisa continuar verde
cd frontend && npm run check         # typecheck, lint, checagem de formatação, testes
```

## Licença

[MIT](LICENSE)
