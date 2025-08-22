from os import environ as env
from datetime import datetime
import time
from typing import Union, List, Optional
import re
from urllib.parse import urlencode, urlparse, urlunparse

from fastapi.logger import logger
from fastapi.responses import JSONResponse
from fastapi import status
from bs4 import BeautifulSoup
import httpx

# Local imports
from src.models import Movimentacao, Telemetria, PoloAtivo, PoloPassivo, ResponseSite, ResponseDefault, ResponseError

# Captura variáveis de ambiente e cria constantes
TEMPO_LIMITE = int(env.get('TEMPO_LIMITE', 180))
TENTATIVAS_MAXIMAS_RECURSIVAS = int(env.get('TENTATIVAS_MAXIMAS_RECURSIVAS', 30))

_SPACE = re.compile(r"\s+")
_ID_DOC = re.compile(r"(?:CPF|CNPJ)\s*:\s*([\d\./-]+)", re.IGNORECASE)


_PART_LINHA = re.compile(
    r"""^\s*
        (?P<nome>.+?)                               
        (?:\s*-\s*OAB\s+[A-Z]{2}\d+\s*)?            
        (?:-\s*(?:CPF|CNPJ)\s*:\s*(?P<doc>[\d\./-]+))?  
        \s*\(\s*(?P<tipo>[^)]+?)\s*\)\s*$           
    """,
    re.IGNORECASE | re.VERBOSE
)

_ONCLICK_URL = re.compile(r"openPopUp\([^,]+,\s*'([^']+)'\)", re.IGNORECASE)

_KEEP_DOC_ENDPOINT_SUBSTR = "documentoSemLoginHTML.seam"

# Limpa espaços em branco e caracteres indesejados
def _clean(s: str) -> str:
    if not s:
        return ""
    return _SPACE.sub(" ", s.replace("\xa0", " ")).strip()

# Normaliza URLs removendo :443 redundante
def _normalize_url(u: str) -> str:
    """Remove :443 redundante em https e normaliza."""
    if not u:
        return ""
    try:
        p = urlparse(u.strip())
        if p.scheme == "https" and p.netloc.endswith(":443"):
            p = p._replace(netloc=p.netloc.rsplit(":", 1)[0])
        return urlunparse(p)
    except Exception:
        return u.strip()

# Extrai links de documentos de uma célula da tabela
def _extract_doc_links(td) -> str:
    """
    Retorna links de documento da célula (se houver), deduplicados e normalizados.
    Lê tanto href quanto o onclick (openPopUp).
    Mantém apenas URLs contendo 'documentoSemLoginHTML.seam'.
    """
    if td is None:
        return ""
    links = []
    vistos = set()

    for a in td.find_all("a"):
        href = (a.get("href") or "").strip()
        if href and href != "#":
            u = _normalize_url(href)
            if u and _KEEP_DOC_ENDPOINT_SUBSTR in u and u not in vistos:
                vistos.add(u)
                links.append(u)

        onclick = a.get("onclick") or ""
        m = _ONCLICK_URL.search(onclick)
        if m:
            u = _normalize_url(m.group(1))
            if u and _KEEP_DOC_ENDPOINT_SUBSTR in u and u not in vistos:
                vistos.add(u)
                links.append(u)

    if not links:
        return _clean(td.get_text(" ", strip=True))
    return "; ".join(links)

# Mapeia 'rótulo -> valor' dos blocos .propertyView, pegando a primeira ocorrência.
def _props_por_rotulo_primeira_ocorrencia(soup: BeautifulSoup) -> dict:
    """
    Mapeia 'rótulo -> valor' dos blocos .propertyView, pegando a primeira ocorrência.
    """
    props = {}
    for pv in soup.select(".propertyView"):
        nome_el = pv.select_one(".name, .name label")
        val_el  = pv.select_one(".value")
        if not nome_el or not val_el:
            continue
        rotulo = _clean(nome_el.get_text(" ", strip=True))
        if not rotulo or rotulo in props:
            continue
        valor = _clean(val_el.get_text(" ", strip=True))
        props[rotulo] = valor
    return props

# Retorna o <tbody> de dados (não cabeçalho). PJe costuma usar id terminando em ':tb'.
def _tbody_dados(table: BeautifulSoup) -> Optional[BeautifulSoup]:
    """
    Retorna o <tbody> de dados (não cabeçalho). PJe costuma usar id terminando em ':tb'.
    """
    if not table:
        return None
    tb = table.find("tbody", id=lambda v: v and v.endswith(":tb"))
    if tb:
        return tb
    for tb in table.find_all("tbody"):
        if not tb.find("tr", class_=lambda c: c and "subheader" in c):
            return tb
    return None

# Lê TODAS as linhas de participantes do polo e retorna lista de objetos
def _parse_participantes(tabela_id: str, soup: BeautifulSoup):
    """
    Lê TODAS as linhas de participantes do polo e retorna lista de objetos
    (PoloAtivo ou PoloPassivo) com nome, cpf_cnpj e tipo.
    Ignora cabeçalhos e linhas vazias.
    """
    table = soup.find("table", id=tabela_id)
    if not table:
        return []

    tbody = _tbody_dados(table) or table.find("tbody")
    if not tbody:
        return []

    resultado = []
    for tr in tbody.find_all("tr", class_=lambda c: not c or "subheader" not in c):
        tds = tr.find_all("td")
        if not tds:
            continue

        bloco = _clean(tds[0].get_text(" ", strip=True))
        if not bloco:
            continue

        if bloco.lower().startswith("participante") and "situação" in bloco.lower():
            continue

        bold = tds[0].find("span", class_="text-bold")
        if bold:
            bloco = _clean(bold.get_text(" ", strip=True))

        m = _PART_LINHA.match(bloco)
        if m:
            nome = _clean(m.group("nome"))
            doc  = _clean(m.group("doc") or "")
            tipo = _clean(m.group("tipo"))
        else:
            docm = _ID_DOC.search(bloco)
            tipom = re.search(r"\(([^()]*)\)\s*$", bloco)
            doc = _clean(docm.group(1)) if docm else ""
            tipo = _clean(tipom.group(1)) if tipom else ""
            nome = _clean(re.sub(r"-\s*(?:CPF|CNPJ).*", "", bloco))
            nome = _clean(re.sub(r"\(.*?\)\s*$", "", nome))

        if not nome or nome.lower().startswith("participante"):
            continue

        if "PoloAtivo" in tabela_id:
            resultado.append(PoloAtivo(nome=nome, cpf_cnpj=doc, tipo=tipo))
        else:
            resultado.append(PoloPassivo(nome=nome, cpf_cnpj=doc, tipo=tipo))

    return resultado

# Lê TODAS as movimentações e retorna lista de objetos
def _parse_todas_movimentacoes(soup: BeautifulSoup) -> List[Movimentacao]:
    """
    Varre todas as tabelas de movimentações (id contendo 'processoEvento') no HTML concatenado.
    Deduplica por (data_hora, descricao, documentos).
    """
    movimentos: List[Movimentacao] = []
    vistos = set()

    tabelas = soup.find_all("table", id=lambda v: v and "processoEvento" in v)
    for table in tabelas:
        tbody = table.find("tbody")
        if not tbody:
            continue
        for tr in tbody.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue

            col1 = _clean(tds[0].get_text(" ", strip=True))
            data_hora, descricao = "", col1
            if " - " in col1:
                data_hora, descricao = [_clean(x) for x in col1.split(" - ", 1)]

            documentos = _extract_doc_links(tds[1]) if len(tds) > 1 else ""

            chave = (data_hora, descricao, documentos)
            if chave in vistos:
                continue
            vistos.add(chave)

            movimentos.append(Movimentacao(
                data_hora=data_hora,
                descricao=descricao,
                documentos=documentos
            ))
    return movimentos


# =========================
# Função principal (HTML concatenado)
# =========================

async def capturar_todas_informacoes(html_concat_ou_soup: Union[str, BeautifulSoup]) -> ResponseSite:
    """
    Recebe o HTML concatenado (todas as páginas) e retorna um ResponseSite completo:
      - Cabeçalho (Número, Data da Distribuição, Classe Judicial, Assunto, Jurisdição, Órgão Julgador)
      - Polo Ativo (lista completa de participantes)
      - Polo Passivo (lista completa de participantes)
      - TODAS as movimentações (de todas as páginas), deduplicadas
    """
    soup = html_concat_ou_soup if isinstance(html_concat_ou_soup, BeautifulSoup) else BeautifulSoup(html_concat_ou_soup, "html.parser")

    props = _props_por_rotulo_primeira_ocorrencia(soup)
    numero_processo   = props.get("Número Processo", "")
    data_distribuicao = props.get("Data da Distribuição", "")
    classe_judicial   = props.get("Classe Judicial", "")
    assunto           = props.get("Assunto", "")
    jurisdicao        = props.get("Jurisdição", "")
    orgao_julgador    = props.get("Órgão Julgador", "")

    # Polos: listas completas
    ativos   = _parse_participantes("j_id134:processoPartesPoloAtivoResumidoList", soup)
    passivos = _parse_participantes("j_id134:processoPartesPoloPassivoResumidoList", soup)

    # Movimentações
    movimentacoes = _parse_todas_movimentacoes(soup)

    return ResponseSite(
        numero_processo=numero_processo,
        data_distribuicao=data_distribuicao,
        classe_judicial=classe_judicial,
        assunto=assunto,
        jurisdicao=jurisdicao,
        orgao_julgador=orgao_julgador,
        polo_ativo=ativos,
        polo_passivo=passivos,
        movimentacoes=movimentacoes
    )

def normalizar_numero_processo(valor: str) -> str:
    # Remove tudo que não for dígito
    digitos = re.sub(r'\D', '', valor)
    if len(digitos) == 20:
        # Formata para o padrão CNJ: NNNNNNN-DD.AAAA.J.TR.OOOO
        return f"{digitos[:7]}-{digitos[7:9]}.{digitos[9:13]}.{digitos[13]}.{digitos[14:16]}.{digitos[16:20]}"
    return valor



async def fetch(numero_processo: str, telemetria: Telemetria) -> dict:
    """
    Consulta pública do TJMG (PJe): abre a página inicial, obtém ViewState,
    envia o formulário via POST (AJAX JSF) e retorna as movimentações.
    """
    
    import re
    padrao_processo = r'^\d{7}-\d{2}\.\d{4}\.8\.13\.\d{4}$'
    if not numero_processo or not isinstance(numero_processo, str) or not re.match(padrao_processo, numero_processo):
        numero_processo = normalizar_numero_processo(numero_processo)

    if telemetria.tentativas >= TENTATIVAS_MAXIMAS_RECURSIVAS:
        logger.error("Número máximo de tentativas recursivas atingido.")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={'code': 3, 'message': 'ERRO_SERVIDOR_INTERNO'}
        )

    inicio = time.time()
    logger.info(f'fetch() TJMG iniciou. Processo: {numero_processo} - Tentativa {telemetria.tentativas}')

    base = "https://pje-consulta-publica.tjmg.jus.br"
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
        "Origin": base,
        "Referer": f"{base}/",
    }

    results = None

    async with httpx.AsyncClient(timeout=TEMPO_LIMITE, verify=False, headers=headers) as client:
        try:
            
            
            r0 = await client.get(base, follow_redirects=True)
            soup0 = BeautifulSoup(r0.content, "html.parser")

            viewstate_input = soup0.find("input", {"name": "javax.faces.ViewState"})
            viewstate_value = viewstate_input["value"] if viewstate_input and viewstate_input.has_attr("value") else None

            action_el = soup0.find(id="fPP:j_id236")
            action_id = action_el["id"] if action_el and action_el.has_attr("id") else "fPP:j_id236"

            payload = {
                "AJAXREQUEST": "_viewRoot",
                "_viewRoot": "",
                "fPP:numProcesso-inputNumeroProcessoDecoration:numProcesso-inputNumeroProcesso": numero_processo,
                "inputNumeroProcesso": "",  
                "mascaraProcessoReferenciaRadio": "on",
                "fPP:j_id150:processoReferenciaInput": "",
                "fPP:dnp:nomeParte": "",
                "fPP:j_id168:nomeSocial": "",
                "fPP:j_id177:alcunha": "",
                "fPP:j_id186:nomeAdv": "",
                "fPP:j_id195:classeProcessualProcessoHidden": "",
                "tipoMascaraDocumento": "on",
                "fPP:dpDec:documentoParte": "",
                "fPP:Decoration:numeroOAB": "",
                "fPP:Decoration:j_id230": "",
                "fPP:Decoration:estadoComboOAB": "org.jboss.seam.ui.NoSelectionConverter.noSelectionValue",
                "fPP": "fPP",
                "autoScroll": "",
                "javax.faces.ViewState": viewstate_value or "",
                "fPP:j_id236": action_id,  
                "AJAX:EVENTS_COUNT": "1",
            }

            body_encoded = urlencode(payload)
            headers_post = headers | {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }
            url_post = f"{base}/pje/ConsultaPublica/listView.seam"

            r1 = await client.post(url_post, data=body_encoded, headers=headers_post, follow_redirects=True)

            soup1 = BeautifulSoup(r1.content, "html.parser")
            
            if 'Ver detalhes do processo' in soup1.text:
            
                link_tag = soup1.find("a", {"title": "Ver Detalhes"})
                detalhe_url = None
                if link_tag and "onclick" in link_tag.attrs:
                    import re
                    match = re.search(r"openPopUp\('Consulta pública','(.*?)'\)", link_tag["onclick"])
                    if match:
                        detalhe_url = f"{base}{match.group(1)}"
                        
                        
                logger.info(f"Acessando página de detalhes: {detalhe_url}")
                resp_processo = await client.get(detalhe_url, headers=headers, follow_redirects=True)

                for cookie_name, cookie_value in resp_processo.cookies.items():
                    logger.info(f"Cookie recebido: {cookie_name}")

                soup_primeira_pagina = BeautifulSoup(resp_processo.content, "html.parser")
                html_paginas = [str(soup_primeira_pagina)]  
                soup_processo = soup_primeira_pagina  

                total_paginas = 1
                page_field_name = None
                pagination_form_id = None
                ajax_container = None
                view_state = None
                
                total_span = soup_processo.find('span', {'class': 'pull-right text-muted'})
                if total_span:
                    match = re.search(r'(\d+)\s+resultados', total_span.text)
                    if match:
                        total_resultados = int(match.group(1))
                        total_paginas = (total_resultados + 14) // 15
                        logger.info(f"Total de resultados: {total_resultados}, páginas: {total_paginas}")
                
                slider_table = soup_processo.find('table', {'class': 'rich-inslider'})
                if slider_table:
                    page_field_name = slider_table.get('id')
                    
                    form = slider_table.find_parent('form')
                    if form:
                        pagination_form_id = form.get('id')
                        
                        action = form.get('action', '')
                        container_match = re.search(r"'containerId':'([^']+)'", action)
                        if container_match:
                            ajax_container = container_match.group(1)
                    
                    right_num = slider_table.select_one('td.rich-inslider-right-num')
                    if right_num and right_num.text.strip().isdigit():
                        total_paginas = int(right_num.text.strip())
                
                viewstate_input = soup_processo.find('input', {'name': 'javax.faces.ViewState'})
                if viewstate_input and viewstate_input.has_attr('value'):
                    view_state = viewstate_input['value']
                
                logger.info(f"Total de páginas: {total_paginas}, Campo de paginação: {page_field_name}")
                
                if total_paginas > 1 and page_field_name and pagination_form_id:
                    logger.info(f"Iniciando paginação: {total_paginas} páginas identificadas")
                    
                    for pagina in range(2, total_paginas + 1):  
                        logger.info(f"Preparando requisição para página {pagina}")
                        
                        hidden_inputs = {}
                        for hidden in soup_processo.find_all('input', {'type': 'hidden'}):
                            if hidden.has_attr('name') and hidden.has_attr('value'):
                                hidden_inputs[hidden['name']] = hidden['value']
                        
                        payload_paginacao = {
                            'AJAXREQUEST': ajax_container or 'j_id134:j_id458',
                            'javax.faces.ViewState': view_state or hidden_inputs.get('javax.faces.ViewState') or 'j_id5',
                            f'{pagination_form_id}': pagination_form_id,  
                            f'{page_field_name}': str(pagina),            
                            'autoScroll': '',
                            'AJAX:EVENTS_COUNT': '1'
                        }
                    
                        body_encoded_paginacao = urlencode(payload_paginacao)
                        
                        url_post = detalhe_url
                        
                        form_action = soup_processo.find('form', {'id': pagination_form_id})
                        if form_action:
                            action_url_match = re.search(r"'actionUrl':'([^']+)'", form_action.get('action', ''))
                            if action_url_match:
                                url_post = f"{base}{action_url_match.group(1)}"
                        
                        if not url_post or 'DetalheProcessoConsultaPublica' not in url_post:
                            url_post = f"{base}/pje/ConsultaPublica/DetalheProcessoConsultaPublica/listView.seam"
                            logger.info(f"URL ajustada para: {url_post}")
                        
                        headers_post = headers | {
                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                            "Referer": detalhe_url,
                            "X-Requested-With": "XMLHttpRequest",  
                            "Accept": "application/xml, text/xml, */*; q=0.01",  
                            "Faces-Request": "partial/ajax"  
                        }
                        
                        try:
                            logger.info(f"Buscando página {pagina} de {total_paginas}")
                            logger.info(f"URL: {url_post}")
                            
                            resp = await client.post(url_post, data=body_encoded_paginacao, headers=headers_post, follow_redirects=True)
                            logger.info(f"Status da resposta: {resp.status_code}")
                            
                            if resp.status_code == 200:
                                logger.info(f"Primeiros 300 caracteres da resposta: {resp.text[:300]}")
                                
                                if resp.text.startswith('<?xml'):
                                    logger.info("Recebemos uma resposta XML parcial, extraindo informações relevantes")
                                    
                                    try:
                                        xml_soup = BeautifulSoup(resp.text, "html.parser")  # Use html.parser para XML também
                                        
                                        new_viewstate = xml_soup.find('update', {'id': 'javax.faces.ViewState'})
                                        if new_viewstate and new_viewstate.text:
                                            view_state = new_viewstate.text.strip()
                                            logger.info(f"Novo ViewState extraído do XML (método 1): {view_state}")
                                        
                                        if not view_state:
                                            vs_match = re.search(r'<update id="javax.faces.ViewState"><!\[CDATA\[([^]]+)\]\]>', resp.text)
                                            if vs_match:
                                                view_state = vs_match.group(1)
                                                logger.info(f"Novo ViewState extraído do XML (método 2): {view_state}")
                                    except Exception as xml_error:
                                        logger.error(f"Erro ao processar XML: {xml_error}")
                                    
                                    new_url = detalhe_url
                                    if '?' in new_url:
                                        new_url = new_url.split('?')[0]
                                    
                                    params = {
                                        'page': str(pagina),
                                        'javax.faces.ViewState': view_state
                                    }
                                    new_url = f"{new_url}?{urlencode(params)}"
                                    
                                    logger.info(f"Fazendo GET para URL atualizada: {new_url}")
                                    resp = await client.get(new_url, headers=headers, follow_redirects=True)
                                
                            soup_processo = BeautifulSoup(resp.content, "html.parser")
                            html_paginas.append(str(soup_processo))  
                            
                            if pagina > 2:
                                current_page_indicator = soup_processo.find('span', {'class': 'currentPage'})
                                if current_page_indicator:
                                    logger.info(f"Página atual: {current_page_indicator.text.strip()}")
                                else:
                                    logger.warning("Não foi possível identificar o indicador de página atual")
                            viewstate_input = soup_processo.find('input', {'name': 'javax.faces.ViewState'})
                            if viewstate_input and viewstate_input.has_attr('value'):
                                view_state = viewstate_input['value']
                                logger.info(f"ViewState atualizado: {view_state}")
                        
                        except Exception as e:
                            logger.error(f"Erro ao buscar página {pagina}: {str(e)}")
                            import traceback
                            logger.error(traceback.format_exc())
                            continue
                

                html_todas = ''.join(html_paginas)
                soup_todas_paginas = BeautifulSoup(html_todas, "html.parser")

                site: ResponseSite = await capturar_todas_informacoes(soup_todas_paginas)
                
                results = {
                    'code': 0,
                    'message': 'Processo encontrado',
                    'datetime': str(datetime.now()),
                    'results': [site]
                }
                return results
            else:
                results = {
                    'code': 0,
                    'message': 'Nenhum processo encontrado',
                    'datetime': str(datetime.now()),
                    'results': []
                }
        except httpx.RequestError as e:
            logger.error(f"Erro de requisição: {e}")
            telemetria.tempo_total = round(time.time() - telemetria.tempo_total, 2)
            results = JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={'code': 4, 'message': 'ERRO_SERVIDOR_INTERNO', 'telemetria': telemetria.dict()}
            )
        except Exception as e:
            logger.error(f"Erro durante a consulta: {e}")
            if telemetria.tentativas < TENTATIVAS_MAXIMAS_RECURSIVAS:
                logger.info("Tentando novamente...")
                telemetria.tentativas += 1
                return await fetch(numero_processo, telemetria)
            else:
                telemetria.tempo_total = round(time.time() - telemetria.tempo_total, 2)
                results = JSONResponse(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    content={'code': 4, 'message': 'ERRO_SERVIDOR_INTERNO', 'telemetria': telemetria.dict()}
                )
        finally:
            telemetria.tempo_total = round(time.time() - telemetria.tempo_total, 2)
            if results is not None and isinstance(results, dict) and "telemetria" not in results:
                results["telemetria"] = telemetria.dict()
        
        return results
