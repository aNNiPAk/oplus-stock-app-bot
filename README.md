# OPlus Stock App Bot

Автоматический pipeline:

`OPlus OTA catalog → official OPlus source URL → selected partitions → stock APK → signature/version check → GitHub Release → Obtainium`

По умолчанию проект настроен на **realme 16 Pro+ RMX5131** и отслеживает:

- `com.oplus.calendar` → репозиторий `oplus-calendar`

Владелец целевого репозитория автоматически берётся из владельца центрального bot-репозитория. При необходимости можно задать GitHub Repository Variable `TARGET_OWNER`.

## Почему отдельный репозиторий на приложение

Для Obtainium это самый простой канал:

`один GitHub repository = один Android package`

Каждый Release содержит ровно один APK. Tag имеет вид:

`v<versionName>-<versionCode>`

Например:

`v16.3.12-1603012`

## 1. Создание целевых репозиториев

Локально с установленным GitHub CLI:

```bash
gh auth login
scripts/bootstrap_target_repos.sh private
```

Скрипт создаст недостающие private-репозитории из `config.json` и добавит первый README, чтобы у репозитория была default branch.

Можно передать `public`, но системные APK являются проприетарными файлами производителя: перед публичной публикацией стоит проверить условия их распространения. Для личного Obtainium-канала private repositories безопаснее с точки зрения распространения.

## 2. Token для GitHub Actions

В центральном bot-репозитории добавьте Actions secret:

`RELEASE_TOKEN`

Это fine-grained GitHub PAT с доступом к целевым репозиториям и правом **Contents: Read and write**.

Если все releases публикуются в текущий repository, встроенного `GITHUB_TOKEN` достаточно, но стандартная конфигурация использует отдельные repositories.

## 3. Запуск

Workflow:

`Actions → Update OPlus stock apps → Run workflow`

Поля необязательны:

- `ota_url_override` — прямой Full OTA URL, если нужно временно обойти автоматический catalog lookup.
- `dry_run` — извлечь APK, сравнить версию и подпись, но не создавать Release.

Также workflow запускается автоматически каждый день.

## 4. Автоматический поиск OTA

`config.json` содержит:

```json
{
  "device": {
    "model": "RMX5131",
    "catalog_region": null
  }
}
```

`catalog_region: null` означает: получить последние записи всех регионов данного model и выбрать самую новую.

Если нужен строго определённый регион, укажите его реальное значение из OTA catalog, например `EU`, только после того как убедитесь, что именно так он обозначен для нужной firmware branch.

Catalog API хранит исходный `source_url`. Для OPlus это может быть `downloadCheck`, который перенаправляет на короткоживущий CDN URL. Bot разрешает ссылку заново перед каждой попыткой извлечения partition.

## 5. Экономия диска GitHub runner

Bot не скачивает полный OTA целиком.

Для каждого partition:

1. Получает свежий CDN URL.
2. `android-payload-dumper` через HTTP Range извлекает только выбранный partition.
3. EROFS/ext4 распаковывается локально.
4. Ищутся отслеживаемые packages.
5. Partition image и распакованная filesystem сразу удаляются.

Порядок partitions задаётся в `config.json`. Когда все отслеживаемые packages найдены, оставшиеся partitions не извлекаются.

## 6. Проверки перед публикацией

Для найденного APK Bot получает:

- Android package name;
- `versionName`;
- `versionCode`;
- SHA-256 APK;
- SHA-256 signing certificate;
- исходный partition и путь.

Если в целевом repository уже есть Release, Bot скачивает предыдущий APK и проверяет:

1. package name совпадает;
2. signing certificate совпадает;
3. новый `versionCode` строго больше предыдущего.

Если certificate изменился, release **не публикуется**.

### Первая публикация

На первом Release предыдущего APK ещё нет, поэтому автоматического certificate baseline тоже нет.

Если известен SHA-256 сертификата установленного приложения, его можно жёстко закрепить:

```json
{
  "package": "com.oplus.calendar",
  "repository": "oplus-calendar",
  "enabled": true,
  "required_certificate_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

После первого проверенного Release последующие обновления всё равно автоматически сверяются с предыдущим APK.

## 7. Добавление других приложений

Добавьте запись в `apps`:

```json
{
  "package": "com.oplus.calendar",
  "repository": "oplus-calendar",
  "enabled": true,
  "required_certificate_sha256": null
}
```

`repository` можно задавать коротким именем: тогда используется `TARGET_OWNER`.

После изменения списка запустите:

```bash
scripts/bootstrap_target_repos.sh private
```

## 8. Obtainium

Для private GitHub repositories добавьте GitHub Personal Access Token в source-specific settings Obtainium.

После этого добавьте URL каждого target repository. Поскольку в repository находится только одно приложение и каждый Release содержит один APK, дополнительные APK filters обычно не нужны.

## Ограничения

- `android-payload-dumper` работает с Full OTA payload. Incremental/delta payload без предыдущих partition images не поддерживается.
- Split APK package автоматически не публикуется: один `base.apk` может быть неустанавливаемым.
- OPlus может менять OTA endpoints и правила `downloadCheck`.
- OTA catalog является сторонним индексом; APK при этом извлекается из OPlus source URL, сохранённого в catalog.
- Если разные регионы имеют разные signing keys, используйте конкретный `catalog_region` и/или `required_certificate_sha256`.
