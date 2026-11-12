# Exercícios de recuperação — PG 13/03 (PRD)

Ferramenta em Python para executar e registrar os exercícios semestrais de recuperação da seção 8 do PG 13/03 Rev.01 (E1 CAL®, E2 Onegreen, E3 Portal; frentes A, B e C), seguindo a *Referência dos Exercícios de Recuperação*.

Os scripts orquestram a AWS, gravam os três marcos de cronometragem, aplicam as salvaguardas da seção 8 e geram o registro em .docx. A verificação de integridade e os comandos T-SQL (restore/backup nativos) são **manuais**: o script gera os arquivos `.sql`, você executa no SSMS e informa os resultados.

## Estrutura

Uma pasta por sistema, um script por cenário (frente) e um script do sistema. Todos usam a biblioteca comum `prd_lib`, que deve ficar ao lado das pastas dos sistemas.

```
Scripts dos Exercícios/
├── prd_lib/                         biblioteca comum (não executar diretamente)
├── requirements.txt
├── CAL/                             E1 — Pablo Souza; Lucas Silva
│   ├── config_cal.exemplo.yaml
│   ├── cal.py                       verificar · situacao · limpar · relatorio
│   ├── frente_a_pitr.py             Frente A — PITR do cofre primário (7.4)
│   ├── frente_b_bak.py              Frente B — .bak do dia anterior (7.6)
│   └── frente_c_cofre_isolado.py    Frente C — cópia do cofre isolado (7.5)
├── Onegreen/                        E2 — Leonardo Lage; Eduardo César
│   ├── config_onegreen.exemplo.yaml
│   ├── onegreen.py
│   ├── frente_a_pitr.py
│   ├── frente_b_banco_cliente.py    Frente B — banco de um cliente (7.7)
│   └── frente_c_cofre_isolado.py
└── Portal/                          E3 — Jefferson Diniz; Leonardo Lage
    ├── config_portal.exemplo.yaml
    ├── portal.py
    ├── frente_a_pitr.py
    ├── frente_b_bak.py
    └── frente_c_cofre_isolado.py
```

Cada sistema tem a sua configuração e a sua pasta `evidencias/`. Os scripts recusam configuração de outro sistema (ex.: rodar o `CAL/frente_a_pitr.py` com o `config_onegreen.yaml`).

## Instalação

Requisitos: Python 3.10+, AWS CLI v2 com perfis configurados (SSO ou chaves) para a conta do sistema e, na Frente C, para a conta de backup isolada; acesso via SSMS à sub-rede de teste.

```
pip install -r requirements.txt
cd CAL
copy config_cal.exemplo.yaml config_cal.yaml      (Linux/macOS: cp)
```

Preencha o `config_<sistema>.yaml`: conta, perfil, cofre primário, sub-rede e grupo de segurança de teste, chave KMS, bucket dos .bak (ou de exportação, no Onegreen), tabelas de referência da integridade e `custo_hora_usd`. A seção `conta_backup` só é necessária para a Frente C (Custodiante). Em `bak.bancos`, o `prefixo` é o caminho dos .bak dentro do bucket e aceita `{ano}`, `{mes}` e `{dia}` da data do arquivo procurado — ex.: `prefixo: "{ano}_{mes}/BACKUP_AUDIT_PORTAL_"` (entre aspas, por causa das chaves). No dia 1º, o .bak do dia anterior é procurado na pasta do mês anterior. As metas já vêm do PCN, seção 7 — confira a cada revisão do PG 13/13.

## Uso (exemplo do CAL®; Onegreen e Portal são iguais, na pasta de cada um)

```
cd CAL
python cal.py verificar                    # prontidão, somente leitura — rodar antes das frentes
python frente_a_pitr.py                    # Frente A
python frente_b_bak.py                     # Frente B
python frente_c_cofre_isolado.py           # Frente C (Custodiante da Conta de Backup)
python cal.py situacao                     # o que já foi feito no ciclo
python cal.py relatorio                    # registro .docx do exercício
python cal.py limpar                       # exclui temporárias que ficaram pendentes
```

Opções:

| Opção | Scripts | Uso |
|---|---|---|
| `--ciclo 2027-1` | todos | semestre (padrão: o atual) |
| `--config arquivo.yaml` | todos | padrão: `config_<sistema>.yaml` na pasta do script |
| `--executante "Nome"` | frentes | padrão: pergunta, sugerindo o titular |
| `--instante 2027-03-15T10:00:00-03:00` | `frente_a_pitr.py`, `frente_b_banco_cliente.py` | instante definido antes do início (padrão: último restaurável no momento da preparação) |
| `--medir-renomeacao` | `frente_a_pitr.py` | mede também o tempo de renomeação, renomeando a instância **temporária** depois da integridade (padrão: não mede) |
| `--data-bak 2027-03-14` | `frente_b_bak.py` | data do .bak (padrão: dia anterior) |
| `--banco-cliente NOME` | `frente_b_banco_cliente.py` | banco do cliente (padrão: pergunta) |
| `--marco2-manual` | `frente_b_*.py` | não usa a Lambda: gera o `.sql` para o SSMS e pede os horários |

`python <script>.py --help` mostra as opções de cada um. Se a execução for interrompida (Ctrl+C, queda de rede, recusa), rode o mesmo comando: ela retoma do passo seguinte ao último concluído.

## O que cada frente faz

**Frente A — PITR** (item 7.4): restaura o backup contínuo da produção para o instante definido em instância temporária (M1) → espera `available` (M2) → integridade manual (M3) → (opcional, `--medir-renomeacao`) renomeia a **temporária** (`<id>-ren`) e cronometra o tempo, para projetar o RTO real com a renomeação → exclui a temporária. Recomendado no E1, onde a meta de 1h20 é apertada.

**Frente B — .bak** (CAL®, Portal; item 7.6): localiza o .bak do dia anterior e sua versão/Object Lock → cria instância nova com criptografia, grupo de opções com `SQLSERVER_BACKUP_RESTORE` e senha no Secrets Manager (M1) → restore nativo pela Lambda, que acompanha o `rds_task_status` até `SUCCESS` (M2; sem Lambda, gera `restore_frente_B.sql` e você informa os horários) → integridade (M3) → teste de imutabilidade → exclusão.

**Frente B — banco de um cliente** (Onegreen; item 7.7): PITR em temporária (M1) e, em paralelo, uma segunda instância isolada no papel da produção → backup nativo do banco do cliente e restore na instância isolada pela Lambda (M2; sem Lambda, `backup_cliente.sql` e `restore_frente_B_cliente.sql` no SSMS). Com a Lambda, a senha da PITR temporária (herdada da produção) passa antes ao Secrets Manager, para a Lambda nunca usar credencial da produção; esses minutos entram no RTO e aparecem destacados no registro → integridade do banco do cliente (M3) → exclusão das duas instâncias e de todas as versões do .bak exportado (dados de cliente).

**Frente C — cofre isolado** (item 7.5): localiza o ponto mais recente do produto no cofre isolado → `start_copy_job` com o perfil do Custodiante (M1; o tempo da cópia é registrado à parte e entra no RTO) → confere a chave KMS do snapshot copiado na conta de destino → restaura em temporária (M2) → integridade (M3) → exclusão (e, se confirmado, do ponto copiado).

RTO = M1 → M3. RPO observado = M1 − instante do ponto restaurado (PITR, gravação do .bak ou criação do ponto no cofre isolado).

## Marco 2 automático (Lambda)

O restore e o backup nativos do SQL Server só existem como procedimentos T-SQL (`rds_restore_database`, `rds_backup_database`), sem chamada equivalente na API da AWS. A Lambda `ius-lmb-exr-sql`, na sub-rede de teste, executa esses procedimentos na instância temporária e devolve o `task_id` e os horários do `rds_task_status`; o script a chama e acompanha a tarefa até `SUCCESS`. Com `teste.lambda_sql` vazio ou `--marco2-manual`, o Marco 2 continua manual.

**Isolamento da produção**

- O grupo de segurança da Lambda só tem saída TCP 1433 para o grupo de segurança de teste; ela não alcança a produção nem a internet.
- A Lambda recusa instâncias sem o prefixo `ius-rds-exr-`, endereços que não sejam o endpoint dessa instância, instâncias no ar há mais horas que o exercício e, no restore, bancos que já existam.
- A senha é a do segredo gerenciado da temporária. O script lê o segredo e a envia na chamada; ela não é gravada em log, registro ou console. O `verificar` confere essas regras e que a produção não aceita conexões da Lambda.

**Implantação (uma vez em cada conta: CAL®, Onegreen, Portal)**

```
cd lambda
python empacotar.py                                   # gera ius-lmb-exr-sql.zip (o pacote já vem pronto)
aws s3 cp ius-lmb-exr-sql.zip s3://<bucket-da-conta>/lambda/ius-lmb-exr-sql.zip --profile <perfil>
aws rds describe-db-subnet-groups --db-subnet-group-name <subnet-group-de-teste> --profile <perfil>   # VPC e sub-redes
aws cloudformation deploy --template-file lambda_sql_nativo.yaml --stack-name ius-cfn-exr-lmb ^
    --capabilities CAPABILITY_NAMED_IAM --profile <perfil> --parameter-overrides ^
    VpcId=<vpc> SubnetIds=<subnet-a>,<subnet-b> SgTesteId=<sg-de-teste> BucketCodigo=<bucket-da-conta>
```

(No Linux/macOS, troque `^` por `\`.) Depois preencha `servico.teste.lambda_sql: ius-lmb-exr-sql` no `config_<sistema>.yaml` e rode `python <sistema>.py verificar`, que testa a função (`ping`), as regras de rede e o isolamento da produção.

O template cria: a função (Python 3.12, `pymssql` 2.4.6), o papel `ius-rol-exr-lmb` (só logs e interfaces de rede), o grupo de segurança `ius-sgr-exr-lmb`, a regra de entrada 1433 no SG de teste a partir dele e o grupo de logs com retenção de 90 dias. Sem endpoint de VPC: a Lambda não chama APIs da AWS.

## Impacto na produção

Os scripts não executam nenhuma operação de escrita em recurso de produção. Na AWS, a produção é apenas consultada (`describe`): o PITR e o restore de snapshot leem os backups já armazenados, não a instância; a cópia do cofre isolado e o .bak usam o AWS Backup e o S3. Nenhum script renomeia, reinicia, modifica ou exclui a instância de produção, seus grupos de parâmetros/opções, grupos de segurança ou DNS.

Os pontos de contato com a produção são manuais, no SSMS, e foram protegidos:

- **Integridade:** o script da produção (`integridade_*_producao.sql`) lê só metadados (`sys.dm_db_partition_stats`) e não varre tabelas. O `COUNT(*)` fica apenas no script da instância restaurada. A consulta funcional vai comentada no script da produção: rode só se for leve.
- **Restore e backup nativos:** os `.sql` gerados só executam se a instância estiver no ar há poucas horas (a do exercício acabou de ser criada) e, no restore, se o banco ainda não existir. Conectado à produção por engano, o comando para com erro.


- O perfil precisa estar na conta configurada; conta diferente interrompe.
- Nenhuma alteração ou exclusão aceita identificador de produção (`ius-mssql-cal`, `medium-onegreen-rds` e `db-portal-fornecedores` são protegidos em qualquer script, mais os `identificadores_protegidos` do arquivo). Só se altera/exclui instância com o prefixo `ius-rds-exr-` **e** a tag `PRD-Exercicio`.
- Antes de criar qualquer recurso: a sub-rede de teste não pode ter rota para internet gateway e os SGs não podem aceitar entrada de `0.0.0.0/0` ou `::/0` (lacunas do E1 de 20/09/2026).
- Temporárias: `PubliclyAccessible=false`, Multi-AZ desligado, criptografia (herdada no PITR/snapshot; `kms_key_id` nas criadas do zero), sem proteção contra exclusão e sem a tag `IUS-BKP-RES` (se herdada, é removida e registrada como lacuna).
- Teste de imutabilidade: só tenta excluir a versão se o objeto reportar Object Lock com retenção futura; exige digitar `TESTAR IMUTABILIDADE`. A simulação IAM confere se o perfil tem `s3:DeleteObjectVersion`, para distinguir recusa por Object Lock de recusa por falta de permissão.
- Toda criação, renomeação e exclusão pede confirmação.

## Evidências

```
CAL/evidencias/E1_2027-1/
  frente_A/registro.json      estado e resultado (base do relatório)
  frente_A/eventos.jsonl      trilha cronológica: chamadas AWS, confirmações, status
  frente_A/integridade_frente_A.sql / .csv
  frente_B/restore_frente_B.sql ...
  verificacao_AAAAMMDD_HHMMSS.json
  notas_exercicio.yaml        ações, NC, correção de RTO/RPO, Coordenador do PCN
  E1_2027-1 - Registro do Exercício de Recuperação.docx
```

Preencha `notas_exercicio.yaml` (criado no primeiro `relatorio`) e gere o relatório de novo. Copie a pasta para *Comitê de Segurança da Informação > Evidências ITs PGs > PG 13_03 > Exercícios de Recuperação*.

## Permissões AWS necessárias

Conta do serviço: `rds:Describe*`, `rds:RestoreDBInstanceToPointInTime`, `rds:RestoreDBInstanceFromDBSnapshot`, `rds:CreateDBInstance`, `rds:ModifyDBInstance`, `rds:DeleteDBInstance`, `rds:AddTagsToResource`, `rds:RemoveTagsFromResource`, `ec2:DescribeRouteTables`, `ec2:DescribeSecurityGroups`, `kms:DescribeKey`, `kms:CreateGrant` (na chave de teste), `secretsmanager:CreateSecret` (senha gerenciada), `s3:ListBucket`, `s3:GetObject*` (head), `s3:ListBucketVersions`, `s3:DeleteObject*` (exportação do Onegreen), `backup:DescribeBackupVault`, `backup:DescribeRecoveryPoint`, `backup:DeleteRecoveryPoint`. Com a Lambda (Frente B): `lambda:InvokeFunction` e `lambda:GetFunctionConfiguration` na `ius-lmb-exr-sql` e `secretsmanager:GetSecretValue` nos segredos gerenciados das temporárias — sugestão de condição: `secretsmanager:ResourceTag/aws:rds:primaryDBInstanceArn` igual a `arn:aws:rds:us-east-1:<conta>:db:ius-rds-exr-*`, para o perfil nunca ler o segredo da produção. Implantação do template: `cloudformation:*` na pilha, `iam:CreateRole`/`iam:AttachRolePolicy`/`iam:PassRole` do `ius-rol-exr-lmb`, `lambda:CreateFunction`, `ec2:CreateSecurityGroup`/`ec2:AuthorizeSecurityGroup*`, `logs:CreateLogGroup`. Perfil de exclusão (`bak.perfil_exclusao`): `s3:DeleteObjectVersion` no bucket dos .bak e, opcionalmente, `iam:SimulatePrincipalPolicy` + `iam:GetRole`.

Conta de backup isolada: `backup:ListRecoveryPointsByBackupVault`, `backup:StartCopyJob`, `backup:DescribeCopyJob`, `iam:PassRole` do papel de cópia; o cofre primário precisa de política de acesso que permita `backup:CopyIntoBackupVault` a partir da conta isolada.

## Pontos a validar na primeira execução real

A ferramenta foi testada em simulação (moto) para todas as frentes; não foi executada contra a AWS da Ius.

1. **PITR pela API do RDS.** A Frente A usa `restore_db_instance_to_point_in_time`, que restaura do mesmo backup contínuo gerenciado pelo AWS Backup. O PG 13/03 descreve o restore pelo console do AWS Backup; o resultado e os tempos são equivalentes, mas confirme que a conta aceita o restore pela API do RDS.
2. **Restore do snapshot `awsbackup:` pela API do RDS** (Frente C). Confirme que `restore_db_instance_from_db_snapshot` aceita o snapshot copiado; se não aceitar, o restore pode ser feito pelo console do AWS Backup, com os horários informados manualmente.
3. **Mensagem do AccessDenied** no teste de imutabilidade e a simulação IAM do perfil de exclusão.
4. **Grupo de opções** usado nas instâncias criadas do zero: o papel IAM do `SQLSERVER_BACKUP_RESTORE` precisa ler o bucket do .bak (CAL®/Portal) e gravar no bucket de exportação (Onegreen).
5. **Lambda:** a Lambda e o `pymssql` foram testados com um SQL Server simulado, não com o RDS. Confirmar na primeira execução a conexão TLS (se o grupo de parâmetros tiver `rds.force_ssl = 1`), o formato do `rds_task_status` e o fuso dos horários `created_at`/`last_updated`: o script compara com o horário em que observou a conclusão e registra lacuna se a diferença passar de 2 minutos. Se algo falhar antes de a tarefa começar, o script oferece seguir no modo manual.

Rode `verificar` alguns dias antes de cada exercício: ele não cria nada e mostra o que falta.
