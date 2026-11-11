# hesteron_nav_sync — NAV bejövő számlák → cikk-egyeztetés → bevételezés

Frappe/ERPNext (v16) app. A NAV Online Számla rendszerből lekéri a
**cég adószámára kiállított (bejövő) számlákat**, a tételeiket **egyezteti a meglévő cikkekkel**,
és egy kattintásra **bevételezés (Purchase Receipt) piszkozatot** készít belőlük — ahol nem biztos a
gép, ott megmondja, mit kell eldönteni.

Csak olvas a NAV-ból (`queryInvoiceDigest`, `queryInvoiceData`, `queryTaxpayer`). Soha nem küld be
számlát, és soha nem küld be (submit) bizonylatot ERPNextben — minden, amit létrehoz, piszkozat.

**Licenc:** MIT. A NAV technikai felhasználó adatai (jelszó, aláíró kulcs, cserekulcs) csak a site
`NAV Connection Settings` jelszómezőiben tárolódnak, a kódban nincsenek.

---

## 1. Felhasználóknak — mit csinál, hogyan használd

### A folyamat

1. Óránként (vagy a „Szinkron futtatása most” gombra) lekéri a NAV-tól az új bejövő számlákat.
2. Minden számlából egy **„NAV Inbound Invoice Import”** rekord lesz, tételes táblázattal. Minden
   sornál látszik: NAV megnevezés, mennyiség, ár, és **milyen cikkre egyeztette, mennyire biztos benne,
   miért**.
3. A sor állapota:
   - **Egyeztetve** (≥ 90 %) — nem kell hozzányúlni.
   - **Javaslat** (60–89 %) — egy kattintás („Javaslatok elfogadása”), vagy válassz másik cikket a soron.
   - **Új cikk** — nincs ilyen cikked. A sor részleteinél az „**Új cikk létrehozása ebből a sorból**” gomb
     előre kitöltött Cikk-űrlapot nyit (név, javasolt cikkcsoport és márka, ME, készletcikk-e). Mentés
     után válaszd ki a soron.
   - **Kihagy** — engedmény-, előleg- vagy negatív sor; ezek nem mennek készletre.
4. Ha minden sor „Egyeztetve”: a rekord állapota **Bevételezhető**, és a **„Bevételezés (Purchase
   Receipt)”** gomb kék. Egy kattintás → kész a bevételezés piszkozata (raktár: a beállításokban megadott
   alapértelmezett raktár vagy a cikk saját alapraktára; nyitott beszerzési rendeléshez kötve, ha volt).
5. **Sorozatszámos cikk**: a sor előre jelzi („bevételezéskor 10 db sorozatszámot kell beolvasni”), és a
   bevételezés piszkozatán a sorozatszám mező nyitva van a szkennelésre. Beküldés (Submit) csak akkor
   megy, ha bent vannak a sorozatszámok — ez az ERPNext saját szabálya, nem kerüljük meg.
6. **Helykód** átjön a bevételezés sorára (custom mező), hogy tudd, hova pakold — csak ha a Cikken
   van `custom_helykód` mező.
7. A bevételezés beküldése után: „**Beszerzési számla a bevételezésből**” → a Purchase Invoice piszkozata
   a NAV számlaszámmal, dátummal, adókategóriával (a bevételezéshez láncolva, ahogy az ERPNext szereti).

### Hogyan talál rá a cikkeidre (sorrendben, az első találat nyer)

| Mód | Mit néz | Biztos |
|---|---|---|
| Tanult szabály | amit korábban egyszer megerősítettél ennél a szállítónál (kód vagy megnevezés) | 100 % |
| Szállítói cikkszám | a Cikk „Beszállítói” táblájában lévő szállítói cikkszám = a NAV-számla termékkódja vagy a megnevezésben lévő kód | 95 % |
| Vonalkód | a Cikk vonalkódja (EAN) = NAV-kód vagy a megnevezésben lévő EAN | 95 % |
| Beszerzési rendelés | nyitott PO-sor ugyanennél a szállítónál (azonos cikk, vagy egyetlen sor azonos mennyiséggel és árral) | 90 % |
| Saját cikkszám a szövegben | pl. „AACBF”, „1205050215” szerepel a megnevezésben | 85 % |
| Hasonló név | megnevezés-hasonlóság a cikk nevével (a szállító korábbi cikkei előnyben) | 60–89 % |

**Ez azt jelenti, hogy a rendszer annál jobb, minél több szállítói cikkszám és EAN van a cikkeken.**
Ami egyszer kézzel lett kiválasztva, azt megjegyzi (`NAV Item Mapping Rule`), és legközelebb már
100 %-kal hozza.

### Amit előre tudni kell

- **Csak magyar (NAV-nak jelentő) szállítók számlái jönnek.** A külföldi
  beszerzés nem jelenik meg a NAV bejövő oldalán — azokat továbbra is kézzel (PO → bevételezés) kell.
- **Módosító / érvénytelenítő számlából nem készül bevételezés** — külön állapotot kap („Módosító
  számla”), és kézzel kell kezelni (visszáru / jóváíró számla).
- **Minden sornak van „Cél”-ja** (0.3.0-tól): *Készletre* (bevételezés, cikk kell) vagy költség-jellegű
  — *Költség*, *Saját felhasználás*, *Beszerzési járulék* —, ami cikk nélkül, egy **költségkategóriával**
  megy egyenesen a beszerzési számlára. A kategóriák (`NAV Expense Category`) mellett a főkönyvi számla
  áll — ezt a könyvelő adja meg. Kategóriánként összevont sor kerül a számlára; a „Szállítási költség”
  fuvarozónként (GLS, DPD, Posta, Foxpost …) bontva.
- **Honnan tudja a célt?** Sorrendben: amit kézzel beállítottál · tanult szabály · kulcsszó a
  megnevezésben (pl. „kerekítés” → Kerekítés, 8699/9699) · a **szállító alapértelmezése** (Szállító
  karton → „NAV import”, vagy a számlán „Műveletek → Szállító alapértelmezése…”) · NAV „szolgáltatás”
  jelölés · különben Készletre. Bármely soron eltérhetsz; amit kézzel átállítasz, megjegyzi.
- **Csak költségsoros számla** (könyvelő, futár, parkolás) → állapota „Rögzíthető”, és a
  „Beszerzési számla (költség)” gomb egy lépésben elkészíti a piszkozatot, a NAV árfolyamával és
  fizetési határidejével. Vegyes számlánál: előbb bevételezés a készletsorokból, a számla abból készül,
  és a költségsorok hozzáadódnak.
- **AAM (alanyi adómentes) szállító** számláját a rendszer nem sorolja be ÁFA-kategóriába, mert erre
  nincs még Tax Category — könyvelővel egyeztetve fel kell venni.
- **Ismeretlen szállító**: az „Új szállító létrehozása” gomb előtölti a nevet és az adószámot; a
  Szállító **Tax ID** mezőjében legyen benne a 8 jegyű törzsszám (bármilyen formátumban).

### Beállítás (NAV Connection Settings) — egyszer

Cég · Adószám törzsszáma · NAV technikai felhasználó (név, jelszó, aláíró kulcs, cserekulcs — ezeket
csak te írod be) · softwareId (a rendszer javasol egyet: `HU` + adószám + `NAVSYNC1`; utána ne
változzon) · Alapértelmezett raktár · Költséghely · opcionálisan értesítő e-mail és az első
szinkron kezdődátuma. Utána: **„Kapcsolat tesztelése”** gomb — ha zöld, a négy NAV-adat mind jó.

---

## 2. For the development team

### Install

Frappe Cloud: Bench → Apps → Add App → GitHub → `Balinatorok/hesteron_nav_sync_public`, branch `main` → Deploy →
site → Install App.

```bash
bench get-app https://github.com/Balinatorok/hesteron_nav_sync_public --branch main
bench --site <site> install-app hesteron_nav_sync
bench --site <site> migrate
```
Dependencies: `requests`, `pycryptodome` (pyproject.toml). Python ≥ 3.10, Frappe/ERPNext v16
(uses `use_serial_batch_fields` on Purchase Receipt Item and `make_purchase_invoice` from Purchase
Receipt — both verified against the version-16 source).

### Layout

```
hesteron_nav_sync/
  nav_client/client.py      NAV API v3.0 client (auth, tokenExchange, queryInvoiceDigest/Data, queryTaxpayer)
  nav_client/xml_parser.py  InvoiceData XML -> dict (schema-accurate: nested tax numbers, productCodes, VAT choice…)
  matching.py               item / supplier / PO matching with confidence + explanations
  mapping.py                NAV VAT case -> Tax Category, NAV UOM enum -> ERPNext UOM
  sync.py                   scheduler: digest -> data -> parse -> match -> staging doc
  receipt.py                on click: draft Purchase Receipt; draft Purchase Invoice from submitted PR; rule learning
  install.py                custom field: Purchase Receipt Item.custom_helykod (fetch_from item)
  doctype/
    nav_connection_settings          Single: credentials (Password fields), software id, defaults, cursor
    nav_inbound_invoice_import       staging header (+ .js: review screen buttons)
    nav_inbound_invoice_import_line  child table: one NAV line, match result, item, warehouse, flags
    nav_item_mapping_rule            learned/manual supplier+code|description -> item
hesteron_nav_sync/tests/    pytest, no Frappe needed (fake_frappe.py stubs the few calls used)
```

### What is verified, and how

- **Protocol**: written from the official XSDs + samples in `nav-gov-hu/Online-Invoice` and the
  2026-02-12 interface spec. `tests/test_client.py` checks the request signature against NAV's own
  sample test vector (`sample/API sample/tokenExchange.xml`: known requestId/timestamp/signKey →
  known SHA3-512). Envelope order (`header`, `user`, `software`), `common` namespace
  (`NTCA/1.0/common`), millisecond timestamps, digest query structure (`page`, `invoiceDirection`,
  `mandatoryQueryParams/insDate/dateTimeFrom|To`), 35-day window, empty `queryInvoiceData` result,
  gzip flag, AES-128-ECB token decoding — all covered.
- **Parser**: all 30 official `Data sample/*.xml` files parse; a domestic sale fixture asserts the
  nested `taxpayerId`, VTSZ product code, UOM enum, VAT percentage and a negative discount line.
- **Matching**: `tests/test_matching.py` runs the engine against an in-memory sample dataset
  (rule / supplier part no / barcode / item code in text / open PO line / fuzzy / unknown-with-suggestion /
  service / negative / advance lines, serial-number note, helykód).
- **ERPNext API usage** (`make_purchase_invoice`, `use_serial_batch_fields`, `ignore_pricing_rule`,
  `set_missing_item_details` keeping an explicit `rate`, Item Supplier / Item Barcode field names)
  was checked against the erpnext `version-16` source, not from memory.

### Not verified (needs the test site)

1. Live `tokenExchange` with the real technical user (button on the settings form).
2. Live `queryInvoiceDigest` / `queryInvoiceData` — first with `sync_from_date` a few days back.
3. `create_purchase_receipt` end-to-end on the site: warehouse defaults, PO linkage
   (`purchase_order_item` → ERPNext validates qty against the PO), serialised row behaviour on Submit,
   custom field creation in `after_install` (only if `Item.custom_helykód` exists on that site).
4. UI: the row-detail "Új cikk létrehozása" button injection relies on the grid form DOM
   (`grid_row.wrapper .grid-form-body .form-area`); adjust the selector if the v16 grid markup differs.

### 0.3.0 — line targets and cost routing (2026-09-27)

Two months of real NAV data showed that many invoices are pure costs and a single courier supplier
can produce most of the lines — forcing an Item per line was wrong. New: `routing.py` (frappe-free,
`tests/test_routing.py`) decides each line's target + expense category (manual > learned rule >
keyword > supplier default > NAV SERVICE > stock) and aggregates cost lines per
(category, target, carrier, sign-dependent account) into Purchase Invoice rows **without item_code**
(allowed on Purchase Invoice Item in v16 — verified: `item_code` reqd=0, `item_name`/`uom`/`qty`/`rate`
reqd=1; negative qty is rejected by BuyingController, so negative buckets go in as qty 1 × negative
rate). Direct PI (no stock lines) gets the tax template via `erpnext.accounts.party.set_taxes` with the
NAV-derived Tax Category, `conversion_rate` = NAV exchange rate, `due_date` = NAV payment date, and the
draft's net/VAT are compared to the NAV totals (warning, not error). New doctype `NAV Expense Category`
(seeded by `install.after_migrate`, never overwrites accounts), Supplier custom fields
`nav_default_target` / `nav_expense_category`, `NAV Item Mapping Rule` gained `target` /
`expense_category` (item optional for cost rules). Tests moved to `hesteron_nav_sync/tests/`
(Frappe Cloud took a top-level `tests/` for the app folder); run `python3 -m pytest hesteron_nav_sync/tests/`.

### Earlier drafts — what changed and why (so nobody reverts it)

The first version of this app (2026-09-10) was written without the official schema. Comparing it to
the XSDs and to real ERPNext usage showed these were wrong and are now fixed:
- created a draft **Purchase Invoice** only — no stock movement, so it didn't serve *bevételezés* at all;
  now the primary output is a **Purchase Receipt** (PO-linked when possible), invoice comes from it;
- read `supplierTaxNumber` as a leaf — it's a complex type (`taxpayerId/vatCode/countyCode`), so every
  supplier match would have failed;
- ignored `productCodes` (the best matching key), `lineNatureIndicator`, `invoiceReference`
  (modification invoices), `unitPriceHUF`, discount/advance lines;
- no `<software>` block (mandatory on every request), wrong `common` namespace, no milliseconds;
- AES-decrypted `invoiceData` (it's plain base64/gzip) and did not decrypt the exchange token (that's
  what the exchange key is for);
- treated `nav_invoice_number` as globally unique (only unique per supplier);
- mapped AAM to the TAM tax category (legally distinct).
