# Tess — Desafio técnico

Chat com frontend Next.js/TypeScript e backend Python/FastAPI independentes. Sem pacotes ou tsconfigs na raiz. Supabase fornece PostgreSQL e autenticação; OpenAI Responses fornece o modelo.

## Estado da entrega

| Área | Implementação | Verificação realizada |
|---|---|---|
| Usuários, múltiplas conversas, histórico e configurações | Persistência, isolamento por usuário, streaming, título por IA, exclusão e estados de execução | Supabase real, API e navegador, local e em produção |
| Ferramentas e internet | Busca nativa OpenAI, leitura de páginas HTTPS, cadastro de ferramentas HTTP GET e MCP remotos; nativas próprias Wikipedia, câmbio (ECB) e clima (Open-Meteo) | Geração real em produção: busca nativa com fontes citadas, scraping, MCP (Context7, Microsoft Learn, Exa) e clima/câmbio reais na mesma resposta |
| Imagens e PDFs | Upload privado, normalização, prévia/download e entradas multimodais nativas | Geração real em produção: imagem descrita corretamente e PDF lido com extração e cálculo |
| Compartilhamento | Snapshot público somente leitura, arquivos delimitados, expiração e revogação | Leitura anônima, expiração e revogação (404) verificadas no endereço público |
| Compactação automática | Resumos persistidos, histórico original preservado, consumo contabilizado | Geração real em produção: 4 turnos acumulados, evento de compactação aos 16.675 tokens, resumo persistido e histórico intacto |
| Cap global e auditoria | Reservas transacionais por chamada, consumo e eventos persistidos | Testes de comportamento e PostgreSQL real |
| MCP | Servidores remotos autorizados, descoberta e seleção explícita de ferramentas | Descoberta e execução reais no Context7, Microsoft Learn e Exa (incluindo fetch de PDF por URL) |
| Google Drive/Gmail | OAuth, pesquisa/leitura, refresh e desconexão | Verificado em produção com conta real: busca listou arquivos reais do Drive e o Gmail resumiu e-mails verdadeiros; tokens criptografados no banco |
| Produção | Dockerfiles não-root, healthchecks e configuração Railway | Aplicação pública em https://frontend-production-126c9ad0.up.railway.app com chat, visão, ferramentas, compactação, contabilidade e compartilhamento verificados no endereço público |
| Vídeo | `artifacts/demo-narrado.mp4` (narrado, corte dinâmico) e `artifacts/demo.mp4` (tom completa), 1min42s, 1280×860 | Gravação contínua em produção na conta real: login, sugestões de início, clima e câmbio por ferramentas nativas em paralelo com follow-ups sugeridos, visão de gráfico + PDF em uma mensagem, MCP (Context7), Google Drive e Gmail reais, link público e tema escuro |

**Estado atual:** a aplicação está publicada em URL cloud própria, com o provedor real (OpenAI `gpt-6-luna`) validado em todos os fluxos obrigatórios e diferenciais — incluindo Google Drive/Gmail autorizados com uma conta real. O cadastro é imediato (sem confirmação por e-mail) e o vídeo em `artifacts/demo.mp4` registra os fluxos ponta a ponta em produção.

## Estrutura

```text
frontend/
  src/app/                  # Login, workspace e /share/[token]
  src/components/           # Conversas, anexos, ferramentas, rastros e compartilhamento
  src/lib/                  # API autenticada, protocolo NDJSON e limites de upload
  Dockerfile / railway.json
  package.json / tsconfig.json / .env.example
backend/
  app.py                     # Composição: FastAPI, rotas de conversas/config/saúde, lifespan
  api/deps.py                # Dependência de autenticação (Bearer Supabase)
  core/config.py             # Configuração por ambiente (Config.from_env)
  core/safe_http.py          # Transporte protegido: SSRF, limites de bytes, DNS pinado
  repositories/supabase.py   # Gateway de dados: rpc, rows, histórico, settings, auditoria
  services/chat.py           # Loop Responses, streaming, compactação e contabilização
  services/context.py        # Montagem do contexto com resumos
  services/tools.py          # Ferramentas nativas, web e MCP
  services/connectors.py     # OAuth e ferramentas Google Drive/Gmail
  services/assets.py         # Arquivos privados e snapshots públicos (rotas)
  tests/                     # Suíte pytest do backend
  supabase/migrations/       # Migrações incrementais, em ordem
  supabase/tests/            # Invariantes de chat e conectores, com rollback
  Dockerfile / railway.json
  pyproject.toml / uv.lock / .env.example
deploy/
  railway.py / test_railway.py
artifacts/                  # Artefatos locais, não versionados
  demo.mp4                  # Demonstração local legendada
  demo-*.png                # Evidências visuais
  verification.json         # Resultado do smoke de navegador e suas limitações
  optional-verification.json # Evidências MCP/Google, distinguindo serviço real de fixture
```

## Preparação

Requer Node.js 24+, npm, Python 3.12+ e uv. Para Supabase local, também Docker e Supabase CLI. Para respostas reais, uma chave OpenAI com billing e um modelo Responses compatível com imagens, PDFs e web search.

```bash
npm --prefix frontend ci
uv --directory backend sync --locked
# Somente se os arquivos ainda não existirem:
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local
```

Nesta máquina os arquivos privados já estão preparados para o Supabase local. Eles são ignorados pelo Git. Não os sobrescreva ao copiar exemplos.

### Banco e autenticação

Para desenvolvimento local:

```bash
supabase start --workdir backend --exclude realtime,storage-api,imgproxy,mailpit,postgres-meta,studio,edge-runtime,logflare,vector,supavisor
supabase migration up --workdir backend --local
supabase status --workdir backend
```

Preencha URL e chaves mostradas pelo CLI nos ambientes correspondentes. O projeto local é `tess-chat`; a confirmação de e-mail está desativada apenas no desenvolvimento. Não exponha essa stack local ou suas chaves compartilhadas na internet.

Nesta máquina os pools automáticos de redes Docker estavam esgotados. A rede isolada existente é `tess-chat-local`, em `10.200.0.0/24`: acrescente `--network-id tess-chat-local` ao start aqui. Também foi necessário remover os sufixos de busca DNS herdados pelo resolver do container Kong e recarregá-lo. A correção está no container em execução; se ele for recriado, revise a busca DNS do Docker. `--ignore-health-check` não comprova disponibilidade.

Para um projeto Supabase cloud próprio e autorizado, aplique **todas as migrações em ordem**. Não execute apenas a primeira migração. Se usar o CLI:

```bash
supabase link --workdir backend --project-ref "$SUPABASE_PROJECT_REF"
supabase db push --workdir backend
```

Configure Site URL/redirects de autenticação para a origem utilizada; no ambiente público, habilite confirmação de e-mail e configure a entrega. Não republique projetos existentes que não pertençam a este desafio.

### Variáveis do backend

| Variável | Configuração |
|---|---|
| `SUPABASE_URL` | URL do mesmo projeto usado pelo frontend |
| `SUPABASE_PUBLISHABLE_KEY` | Chave pública publishable ou JWT `anon` legado |
| `SUPABASE_SECRET_KEY` | Chave privada secret ou JWT `service_role` legado; somente servidor |
| `OPENAI_API_KEY` | Chave privada com acesso e billing |
| `OPENAI_MODEL` | Padrão `gpt-4.1-mini`; compatível com os recursos utilizados |
| `MAX_OUTPUT_TOKENS` | Padrão `2048` |
| `CONTEXT_INPUT_TOKENS` | Limiar de compactação, padrão `16000` |
| `CONTEXT_WINDOW_TOKENS` | Janela real do modelo, padrão `128000`; ajuste ao trocar de modelo |
| `SUMMARY_OUTPUT_TOKENS` | Saída máxima por resumo, padrão `1500` |
| `MAX_MODEL_STEPS` / `MAX_TOOL_CALLS` | Padrão `8` para cada limite por execução |
| `ENCRYPTION_KEY` | Chave Fernet estável para credenciais de ferramentas e Google |
| `PUBLIC_APP_URL` | Origem pública do frontend; local preparado em `http://localhost:3005` |
| `MCP_TRUSTED_HOSTS` | Hosts públicos exatos, separados por vírgula; em produção: `mcp.context7.com,learn.microsoft.com,mcp.exa.ai,api.githubcopilot.com,mcp.deepwiki.com` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Cliente OAuth Google, se usar o conector |
| `GOOGLE_REDIRECT_URI` | Retorno exato em `<PUBLIC_APP_URL>/api/connectors/google/callback` |
| `PORT` | Padrão `3001` |

Gere uma chave Fernet uma vez e preserve-a nos backups e próximos deploys:

```bash
uv --directory backend run --locked python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Trocar essa chave torna as credenciais já criptografadas ilegíveis. Não coloque segredos em URLs, no código ou em variáveis `NEXT_PUBLIC_*`. Variáveis exportadas no shell prevalecem sobre o arquivo lido pelo uv; uma chave antiga exportada precisa ser removida ou substituída.

### Variáveis do frontend

`NEXT_PUBLIC_SUPABASE_URL` e `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` apontam para o mesmo Supabase do backend. `BACKEND_URL` aponta para o backend, com padrão `http://127.0.0.1:3001`. Essas variáveis precisam estar corretas **antes do build**, incluindo o endereço usado pelo rewrite `/api/*`.

## Executar

Em terminais separados, na raiz:

```bash
uv --directory backend run --locked --env-file .env python app.py
npm --prefix frontend run dev -- --port 3005
```

Abra http://localhost:3005. O frontend encaminha `/api/*` ao backend. Sem chave do modelo, login, organização, arquivos e compartilhamento continuam disponíveis, mas o envio fica explicitamente desabilitado.

Para reload do backend, mantenha os logs de acesso desativados para não registrar códigos OAuth ou tokens de compartilhamento em URLs:

```bash
uv --directory backend run --locked --env-file .env uvicorn app:app --host 127.0.0.1 --port 3001 --reload --no-access-log
```

Se o npm apresentar `EALLOWSCRIPTS` por configuração externa, use `npm --userconfig=/dev/null --prefix frontend ci`.

## Recursos e limites

- **Arquivos:** PNG/JPEG/WebP e PDF; até cinco por mensagem, 5 MB por arquivo, 20 páginas por PDF e 50 MB por usuário. Imagens são decodificadas, redimensionadas e reencodificadas; PDFs são validados/reconstruídos, com rejeição de criptografia, conteúdo excessivo e formatos incompatíveis. Bytes privados ficam no PostgreSQL, sem bucket público. Uploads pendentes sobrevivem ao reload e podem ser selecionados ou excluídos. Um envio rejeitado mantém a seleção para nova tentativa; a sincronização remove apenas anexos já aceitos no histórico. Anexos já usados não são apagados isoladamente.
- **Compartilhamento:** consentimento explícito, snapshot congelado, validade de 1–7 dias e token aleatório armazenado somente como hash. Novas mensagens e arquivos não entram no snapshot antigo. Revogar bloqueia novas leituras; não remove cópias já baixadas.
- **HTTP/scraping:** somente HTTPS público na porta 443, sem credenciais na URL. O transporte valida DNS/IP no dial, fixa o IP validado e preserva SNI/verificação TLS. Bloqueia destinos privados/locais e limita tamanho, duração e redirects.
- **MCP:** cadastre o servidor desativado, descubra suas ferramentas, selecione nomes permitidos, salve e ative. O host precisa estar em `MCP_TRUSTED_HOSTS`; as ferramentas precisam declarar leitura sem efeitos destrutivos. Credenciais Bearer ficam criptografadas e não voltam ao navegador. Desativação ou alteração é revalidada antes de executar uma ferramenta carregada.
- **Google:** habilite Drive API e Gmail API no projeto Google, configure a tela de consentimento/usuários de teste e autorize o redirect exato. A interface oferece pesquisa e leitura, não envio de e-mail ou escrita em arquivos. OAuth usa PKCE, state de uso único associado ao navegador e credenciais criptografadas; desconectar impede reutilização dos tokens salvos. A leitura Gmail busca corpos MIME externos quando o [MessagePartBody](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments#MessagePartBody) contém `attachmentId`, respeita o charset declarado e não mistura arquivos anexados com o corpo do e-mail.
- **Contexto:** conta as entradas multimodais reais antes da geração. Ao atingir o limiar, resume turnos completos, persiste o resumo e continua no mesmo envio sem apagar mensagens. Também preserva pares de chamada/resultado de ferramentas. Se o próprio envio atual exceder a janela real, retorna erro antes de gerar, sem remover silenciosamente seus arquivos.
- **Execuções:** NDJSON com texto, andamento, ferramentas e compactação; falhas/cancelamentos preservam texto parcial. Há limites de chamadas e duração, idempotência por `request_id`, uma execução ativa por conversa e até dez novas execuções por usuário/minuto.
- **Conversas:** a listagem usa um único snapshot do banco, em ordem de atualização, sem páginas por offset que possam duplicar ou omitir conversas durante renomeações concorrentes. A primeira mensagem gera um **título por IA** (chamada curta reservada e contabilizada); nome escolhido manualmente nunca é sobrescrito e falha na nomeação não interrompe o chat. A exclusão é explícita, em dois cliques, remove mensagens, execuções, anexos e links de compartilhamento e libera reservas de execuções em andamento.

## Usabilidade adicional

- **Buscar conversas:** filtra os títulos no navegador, sem diferenciar maiúsculas ou acentos. A busca não pesquisa o conteúdo das mensagens nem altera a conversa aberta; criar outra conversa limpa o filtro.
- **Copiar mensagens:** copia o texto exato, inclusive em snapshots públicos. Mensagens vazias ou ainda em execução ficam bloqueadas. Se o navegador negar acesso ao clipboard, a interface informa a falha e permite seleção/cópia manual.
- **Exportar:** baixa JSON com mensagens, consumo por execução, rastros de ferramentas e metadados de anexos da conversa selecionada. Não envia a exportação a outro serviço e não inclui binários nem configurações da conta. Não é um backup completo; trate o arquivo com o mesmo sigilo da conversa. A exportação fica bloqueada durante carregamento, troca/sincronização ou execução.
- **Ctrl/Cmd+Enter:** envia pelo mesmo formulário e pelas mesmas verificações do botão. Enter continua inserindo uma quebra de linha; composição de texto por IME não dispara envio.

A interface usa apenas rótulos e avisos necessários. Consumo e rastros ficam em **Saldo** e **Detalhes**; configurações, ferramentas e compartilhamento ficam recolhidos. Durante a atualização, a edição e o salvamento de instruções e nomes de conversas ficam bloqueados.

## Uso e auditoria

O cap inicial é **1.000.000 de tokens para todas as contas**. Ajuste no SQL Editor autorizado:

```sql
update public.usage_accounts set limit_tokens = 100000 where id = 'global';
```

Cada chamada de geração ou resumo reserva entrada + saída máxima antes de acessar o modelo. Ao registrar consumo conhecido, a reserva desse passo é ajustada imediatamente ao uso real, liberando saldo para a próxima chamada; o registro é idempotente. Passos com consumo desconhecido mantêm sua reserva conservadora, sem voltar a cobrar estimativas excedentes dos passos conhecidos. Não há retries automáticos do provedor. Execuções sem heartbeat por mais de dez minutos são recuperadas como interrompidas, sem apagar uso desconhecido.

Se a confirmação de uma reserva falhar antes de chamar o provedor, o encerramento reconcilia a etapa não iniciada e fecha a execução numa única transação, sem cobrar essa chamada. O mesmo lock impede que uma reserva atrasada reabra a execução. Consumo de etapas anteriores é preservado; a recuperação por timeout continua necessária se o banco permanecer inacessível durante o encerramento.

A quota é em **tokens**, não em reais/dólares. Busca nativa e serviços externos podem ter cobranças próprias: configure também os limites financeiros no provedor antes de abrir o acesso público.

O banco persiste perfis, configurações, conversas, mensagens, arquivos, snapshots, ferramentas, conectores, resumos, passos do modelo, execuções, lançamentos de consumo e auditoria. RLS isola usuários; as mutações confiáveis usam funções restritas ao backend. Eventos incluem alterações, anexos, compartilhamento/revogação, ferramentas, conectores, compactação, reservas, consumo e falhas. Chaves, senhas e tokens não são incluídos nos metadados de auditoria. Proteja e retenha também os logs do Supabase Auth; configure proxies para não registrar queries OAuth nem URLs de compartilhamento.

## Verificação

```bash
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
uv --directory backend run --locked python -m unittest -v
python -m unittest discover -s deploy -p 'test_*.py' -v
```

Resultados exercitados: lint/typecheck/build do frontend, quinze testes frontend, 47 testes backend aprovados e um teste condicional de PostgreSQL não executado pelo unittest sem sua variável de conexão. O teste de limites do deploy também passou. O fluxo real de navegador terminou sem exceções de JavaScript; evidências estão em `artifacts/`.

Com Supabase local de desenvolvimento disponível, os checks SQL fazem rollback dos dados de teste:

```bash
docker exec -i supabase_db_tess-chat psql -U postgres -d postgres -v ON_ERROR_STOP=1 < backend/supabase/tests/chat.sql
uv --directory backend run --locked python -c 'import base64; from test_assets import DATABASE_SECURITY_CHECK, png; print(DATABASE_SECURITY_CHECK.replace("__PNG_BASE64__", base64.b64encode(png()).decode()))' | docker exec -i supabase_db_tess-chat psql -U postgres -d postgres -v ON_ERROR_STOP=1
docker exec -i supabase_db_tess-chat psql -U postgres -d postgres -v ON_ERROR_STOP=1 < backend/supabase/tests/connectors.sql
```

Os três checks foram executados contra PostgreSQL real. Cobrem propriedade, RLS, privilégios, quota, snapshots, expiração/revogação, idempotência, consumo desconhecido, recuperação e contabilização de múltiplas chamadas/resumos. A regressão de chat cobre liberação imediata de saldo, uso misto conhecido/desconhecido, consumo reportado acima da estimativa, encerramento antes do provedor e listagens de 1.001 conversas com isolamento de contas. O check de conectores também cobre state OAuth de uso único/expirado, escopos obrigatórios, isolamento de contas, leases de refresh e impossibilidade de um refresh/callback antigo restaurar uma conexão removida ou substituída.

O smoke de conectores exercitou as rotas reais com autenticação e persistência reais, mas respostas HTTP Google de teste: PKCE, replay rejeitado, tokens criptografados, dois leitores concorrentes com um único refresh externo, pesquisa/leitura Drive e Gmail e desconexão mesmo com falha na revogação remota. Não é autorização real de uma conta Google. O MCP foi consultado e executado no servidor Context7 real; resultados estão em `artifacts/optional-verification.json`. Os testes do protocolo do modelo não substituem validar uma geração real com chave válida.

Os extras também foram exercitados no navegador contra autenticação e dados locais reais: busca com acentos, clipboard permitido/negado, exportação sem upload, isolamento entre conversas durante carregamento e layout em 390/320 px. O teste de teclado usou somente uma indicação de prontidão no cliente; os dois envios chegaram à API real e receberam 503 por falta de chave, sem gerar respostas nem consumir créditos. Resultados em `artifacts/extras-verification.json`.

A imagem atualizada do frontend também foi iniciada e validada com login, proxy autenticado ao backend, busca e clipboard. Nesta máquina, o acesso do bridge Docker ao gateway do host expirou; o smoke integrado usou rede host e bind em `127.0.0.1`, sem alterar o firewall. Isso é verificação local, não publicação cloud.

A revisão visual foi verificada em desktop e em 390/320 px, preservando autenticação, anexos, MCP, exportação, consentimento e revogação. Evidências atuais: `artifacts/minimal-*.png` e `artifacts/minimal-verification.json`. O vídeo local existente mostra a aparência anterior; a gravação final será atualizada após a publicação.

As correções de retry e atualização foram exercitadas no frontend reconstruído: seleção preservada após 503 real, retry somente com anexo habilitado, instruções e salvamento bloqueados durante um GET real deliberadamente atrasado, e salvamento posterior confirmado pela API. O smoke alterou apenas a indicação de prontidão do cliente, sem configurar ou chamar o modelo; conta e arquivos de teste foram removidos.

O smoke de confirmação perdida executou o fluxo real do backend e a persistência real, com perda de resposta de reserva injetada e um contador de tokens de teste: a execução terminou como falha, cobrou zero e permitiu novo envio imediato, sem geração do modelo. A listagem devolveu 1.001 IDs distintos mesmo com renomeação entre a leitura do snapshot e sua entrega; o endpoint HTTP autenticado também retornou todos os IDs. No navegador, renomeação e submit programático ficaram bloqueados durante refresh, e o nome salvo depois permaneceu no refresh seguinte. Todos os dados temporários foram removidos.

## Roteiro de demonstração

1. **Login e cadastro**: entre com uma conta própria (ou crie) — o cadastro é imediato, sem confirmação por e-mail, e a primeira conversa surge automaticamente.
2. **Chat real**: envie uma pergunta; a conversa recebe um **título gerado pela IA** e a resposta chega em streaming com Markdown formatado. Use **Regenerar** na última resposta para repetir com nova geração.
3. **Visão e documentos**: anexe uma imagem ou PDF e pergunte sobre o conteúdo (ex.: "resuma este documento em 3 frases").
4. **Ferramentas**: "Pesquise na web X e cite a fonte" (busca nativa); "Use a ferramenta Exa/Microsoft Learn/Context7..." (MCP habilitado em Ferramentas).
5. **Compactação**: cole um texto bem longo (acima de ~16 mil tokens) e observe o evento "Resumindo contexto anterior" preservando o histórico original.
6. **Compartilhamento**: Compartilhar → criar link, abrir em janela anônima (sem login), depois revogar e recarregar o link.
7. **Exportar**: baixe o histórico em JSON ou Markdown; tema claro/escuro no rodapé da barra lateral.
8. **Consumo**: painel Saldo mostra reserva e consumo por execução; Detalhes de cada resposta mostra o modelo e os tokens.

## Publicação cloud

Há Dockerfiles e `railway.json` nos dois serviços. Crie/selecione **dois serviços próprios autorizados** e um Supabase cloud; configure os ambientes acima. O frontend precisa das variáveis públicas e `BACKEND_URL` no build. Use HTTPS, segredo privado somente no backend e os limites financeiros do provedor.

`deploy/railway.py` publica somente em recursos já existentes explicitamente selecionados. Instale a CLI oficial `@railway/cli` e autentique com `railway login` ou um token autorizado. Para cada serviço, forneça `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID` e `RAILWAY_SERVICE_ID`, além das variáveis do componente:

```bash
python deploy/railway.py backend --check-auth
python deploy/railway.py backend
# Troque RAILWAY_SERVICE_ID pelo serviço frontend e configure seu ambiente:
python deploy/railway.py frontend --check-auth
python deploy/railway.py frontend
```

Os segredos são enviados à CLI por stdin, sem imprimi-los. Se o launcher npm não iniciar, repare sua instalação nativa; não trate saída vazia como autenticação bem-sucedida. O runner não cria projetos nem confirma disponibilidade apenas porque o upload terminou.

Depois do deploy, confirme `/api/health` e teste, pelo endereço público, uma resposta real, vision/PDF, ferramentas, compactação, consumo, compartilhamento e OAuth configurado. Atualize o vídeo com esses fluxos reais antes da entrega final ao avaliador. Combine o período de avaliação e programe a desativação conforme o limite de uma semana do enunciado.

### Pendências do responsável

1. Fornecer `OPENAI_API_KEY` válida com billing para verificar os fluxos do modelo.
2. Autorizar os serviços cloud e disponibilizar o Supabase de produção; então publicar e verificar o link público.
3. Para o diferencial Google, fornecer o cliente OAuth e autorizar o redirect/usuários de teste.

Não envie essas credenciais no chat público nem as versione. Configure-as nos ambientes privados dos serviços.
