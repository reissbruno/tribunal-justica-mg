from enum import Enum
from pydantic import BaseModel
from typing import Optional, List

class Movimentacao(BaseModel):
    data_hora: str = ""
    descricao: str = ""
    documentos: str = ""

class PoloAtivo(BaseModel):
    nome: str = ""
    cpf_cnpj: str = ""
    tipo: str = ""

class PoloPassivo(BaseModel):
    nome: str = ""
    cpf_cnpj: str = ""
    tipo: str = ""

class ResponseSite(BaseModel):
    numero_processo: str = ""
    data_distribuicao: str = ""
    classe_judicial: str = ""
    assunto: str = ""
    jurisdicao: str = ""
    orgao_julgador: str = ""
    polo_ativo: List[PoloAtivo] = []      
    polo_passivo: List[PoloPassivo] = []
    movimentacoes: List[Movimentacao] = []
    
    
class ResponseDefault(BaseModel):
    code: int
    message: str
    datetime: str
    results: List[ResponseSite]

class Telemetria(BaseModel):
    tentativas: Optional[int] = 0
    captchas_resolvidos: Optional[int] = 0
    bytes_enviados: Optional[int] = 0
    tempo_total: Optional[float] = 0.0

class ResponseError(BaseModel):
    code: int
    message: str