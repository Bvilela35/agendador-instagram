# Agendador de posts do Instagram

Agenda posts numa conta do Instagram usando a **API oficial da Meta**, de graça e sem limite de
quantidade. Você preenche uma planilha (`posts.csv`), coloca as imagens e vídeos em `midia/`, e o
GitHub Actions verifica a cada 15 minutos o que está na hora e publica.

- Aceita **foto**, **carrossel** (2 a 20 itens) e **reels**.
- Até 100 publicações por 24h por conta (limite da Meta).
- Não precisa de servidor nem de dependências: é um único arquivo Python.

## Como funciona

```
posts.csv + midia/  →  GitHub Actions (a cada 15 min)  →  API do Instagram  →  post no ar
                                    ↓
                         publicados.json (registro do que já saiu)
```

A API do Instagram não recebe arquivos: ela baixa a mídia de um **link público**. Por isso o
repositório no GitHub precisa ser **público**, e o script monta os links `raw.githubusercontent.com`
para as fotos e vídeos de `midia/`. Isso tem dois efeitos:

- As mídias ficam acessíveis por quem tiver o link antes do horário do post.
- Em repositório público o GitHub Actions é grátis e sem limite de minutos.

Se preferir não expor as mídias antes da hora, coloque um link completo (`https://...`) na coluna
`arquivos` apontando para onde você hospeda a imagem (Cloudinary, Google Cloud Storage, etc.).

---

## Configuração (uma vez só, cerca de 20 minutos)

### 1. Conta profissional no Instagram

No app: **Configurações → Tipo de conta e ferramentas → Mudar para conta profissional**
(Criador de conteúdo ou Empresa). Não precisa de página do Facebook.

### 2. App na Meta e token de acesso

1. Acesse [developers.facebook.com/apps](https://developers.facebook.com/apps) e clique em **Criar app**.
2. Em caso de uso, escolha **Gerenciar mensagens e conteúdo no Instagram** (API do Instagram).
3. No painel do app, abra **API do Instagram → Configuração da API com login do Instagram**.
4. Em **Gerar tokens de acesso**, clique em **Adicionar conta** e entre com a conta do Instagram que
   vai receber os posts.
   - Se pedir, adicione a conta em **Funções do app → Testadores do Instagram** e aceite o convite
     no Instagram (**Configurações → Apps e sites → Convites de testador**).
5. Clique em **Gerar token** ao lado da conta e copie. Ele vale **60 dias**.

O app pode ficar em **modo de desenvolvimento**: como só publica na sua própria conta, não precisa
passar pela revisão da Meta.

> Os nomes dos menus no painel da Meta mudam com frequência. Se algo não bater, procure por
> "Instagram API with Instagram Login" e "Generate access token".

### 3. Repositório no GitHub

1. Crie um repositório **público** no GitHub e envie esta pasta inteira para ele (ela é a raiz do
   repositório, com `scheduler.py` e `.github/` direto nela).
2. No repositório: **Settings → Secrets and variables → Actions → New repository secret**.
   - Nome: `IG_ACCESS_TOKEN`, valor: o token do passo 2.
3. Em **Actions**, se aparecer um aviso, clique para ativar os workflows.

### 4. Testar

1. Em **Actions → Publicar posts agendados → Run workflow**, deixe **Só simular** marcado e rode.
   O log mostra o que seria publicado, sem publicar nada.
2. Na sua máquina, você pode conferir a conta conectada:

   ```bash
   echo "IG_ACCESS_TOKEN=cole_o_token_aqui" > .env
   python3 scheduler.py conta
   ```

---

## Uso no dia a dia

### 1. Coloque as mídias em `midia/`

- **Foto e carrossel:** só **JPEG** (`.jpg`). Proporção entre **4:5** (vertical, 1080x1350) e
  **1,91:1** (horizontal). Quadrado 1080x1080 também serve. No carrossel, todas as fotos são
  cortadas na proporção da primeira.
- **Reels:** `.mp4` ou `.mov`, vertical 9:16 (1080x1920), até 15 minutos.
- PNG não funciona. No Mac, converta assim:

  ```bash
  sips -s format jpeg midia/arte.png --out midia/arte.jpg
  ```

### 2. Preencha `posts.csv`

| coluna    | exemplo                         | observação                                       |
|-----------|---------------------------------|--------------------------------------------------|
| `data`    | `2026-12-01` ou `01/12/2026`    |                                                  |
| `hora`    | `18:30`                         | horário de Brasília                              |
| `tipo`    | `foto`, `carrossel` ou `reels`  |                                                  |
| `arquivos`| `dia02-1.jpg\|dia02-2.jpg`      | nomes dentro de `midia/`, separados por `\|`     |
| `legenda` | texto do post                   | pode ter várias linhas, emojis e até 30 hashtags |

Veja `posts.exemplo.csv`. Dá para editar no Google Sheets ou no Excel e exportar como CSV
(vírgula ou ponto e vírgula funcionam).

### 3. Confira antes de enviar

```bash
python3 scheduler.py verificar
```

Mostra cada post com a situação (`agendado`, `publicado`, `erro`...) e aponta problemas: arquivo
faltando, PNG, proporção errada, legenda longa demais.

### 4. Envie para o GitHub

```bash
git add midia posts.csv
git commit -m "Posts de dezembro"
git push
```

Pronto. A cada 15 minutos o GitHub roda o script. O resultado de cada post fica em
`publicados.json`, com o link do post publicado.

---

## Comportamentos importantes

- **Atrasos do GitHub:** o agendamento do GitHub Actions costuma atrasar de 5 a 20 minutos em
  horários de pico. Não dá para garantir o minuto exato.
- **Falhas:** se um post der erro, o script tenta de novo nas próximas rodadas (até 3 vezes) e o
  GitHub manda um e-mail avisando que o workflow falhou. O erro fica em `publicados.json`.
  Para tentar de novo depois de corrigir, apague a entrada desse post em `publicados.json`.
- **Posts esquecidos:** um post com horário passado há mais de 12 horas não é publicado
  (fica como `ignorado`). Isso evita que uma linha com data antiga saia de surpresa.
- **Sem post duplicado:** se a execução cair no meio da publicação, a próxima rodada consulta o
  Instagram antes de tentar de novo.
- **Editar depois:** mudar só a legenda de um post já publicado não republica. Mudar data, hora ou
  arquivos cria um post novo.
- **Inatividade:** o GitHub desliga workflows agendados após 60 dias sem nenhum commit. Os registros
  de publicação contam como commit, mas se ficar 2 meses sem posts, reative em **Actions**.

## Renovar o token (a cada 60 dias)

**Automático (recomendado):** crie um token do GitHub em
[github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens)
→ **Fine-grained token**, só para este repositório, com permissão **Secrets: Read and write**.
Salve como secret `GH_PAT` no repositório. O workflow **Renovar token do Instagram** passa a renovar
sozinho no dia 1 e 15 de cada mês.

**Manual:** gere um novo token no painel da Meta (passo 2) ou rode
`python3 scheduler.py renovar-token` e atualize o secret `IG_ACCESS_TOKEN`.

## Configurações opcionais

Variáveis de ambiente (no `.env` local ou no `env:` do workflow):

| variável           | padrão              | para quê                                      |
|--------------------|---------------------|-----------------------------------------------|
| `FUSO`             | `America/Sao_Paulo` | fuso dos horários do CSV                      |
| `MAX_ATRASO_HORAS` | `12`                | até quantas horas de atraso ainda publica     |
| `MAX_TENTATIVAS`   | `3`                 | tentativas antes de desistir de um post       |
| `IG_API_VERSION`   | `v27.0`             | versão da API do Instagram                    |
| `MEDIA_BASE_URL`   | (definido no workflow) | endereço público da pasta `midia/`         |
