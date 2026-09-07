import sys
import os
import time
from getpass import getpass
from pathlib import Path
from uuid import uuid4
import requests
import yaml
import pytz
from argparse import ArgumentParser
from datetime import datetime, timedelta

BASE_URL = 'https://portal.ufsm.br/mobile/webservice'
API_VERSION = '5.5.7'
REQUEST_TIMEOUT = 30
SCHEDULE_COOLDOWN = 3
CONFIG_FILE = Path(__file__).with_name('config.yaml')
http = requests.Session()
device_id = str(uuid4())
FRIDAY_MONDAY_FALLBACK_KEY = 'use-monday-schedule-on-friday-without-weekend'

def read_config() -> dict:
    with CONFIG_FILE.open('r', encoding='utf-8') as document:
        data = yaml.safe_load(document) or {}

    if not isinstance(data.get('environment'), dict):
        raise ValueError('a seção environment não foi encontrada em config.yaml')
    if not isinstance(data.get('schedules'), list):
        raise ValueError('a seção schedules não foi encontrada em config.yaml')

    behavior = data.get('behavior', {})
    if behavior is None:
        behavior = {}
    if not isinstance(behavior, dict):
        raise ValueError('a seção behavior deve ser um objeto em config.yaml')

    if FRIDAY_MONDAY_FALLBACK_KEY in behavior and not isinstance(behavior[FRIDAY_MONDAY_FALLBACK_KEY], bool):
        raise ValueError(f'o campo behavior.{FRIDAY_MONDAY_FALLBACK_KEY} deve ser true ou false')

    data['behavior'] = behavior
    return data

def is_weekday(date: datetime, weekday: str) -> bool:
    weekdays = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
    return weekdays[date.weekday()] == weekday

def resolve_restaurant_id(restaurant: int):
    match restaurant:
        case 2:
            return 41
        case _:
            return restaurant

def unwrap_response(response) -> object:
    data = response.json()
    if isinstance(data, dict) and 'body' in data:
        if data.get('error'):
            reason = data.get('message') or data.get('mensagem') or data.get('impedimento')
            raise RuntimeError(reason or f'Resposta de erro da API: {data}')
        return data['body']
    return data

def login(username: str, password: str) -> str:
    response = http.post(
        f'{BASE_URL}/flutter/generateTokenJwt',
        json={
            'appName': config['environment']['app'],
            'deviceId': device_id,
            'deviceInfo': config['environment']['device-info'],
            'messageToken': config['environment']['message-token'],
            'login': username,
            'senha': password
        },
        headers={
            'X-UFSM-Access-Name': config['environment']['app'],
            'X-UFSM-Version': API_VERSION,
            'X-UFSM-Device-ID': device_id
        },
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()

    data = unwrap_response(response)

    if isinstance(data, str):
        return data

    if data.get('accessToken'):
        return data['accessToken']

    if data.get('error'):
        raise RuntimeError(data.get('mensagem', 'A API recusou o login.'))

    if not data.get('token'):
        raise RuntimeError('A API não retornou um token de acesso.')
    
    return data['token']

def schedule_meal(token: str, start: datetime, end: datetime, options: dict) -> list:
    payload = {
        'dataInicio': start.strftime('%Y-%m-%d %H:%M:%S'),
        'dataFim': end.strftime('%Y-%m-%d %H:%M:%S'),
        'idRestaurante': resolve_restaurant_id(options['restaurant']),
        'opcaoVegetariana': options['vegetarian'],
        'tiposRefeicoes': []
    }

    if options['coffee']:
        payload['tiposRefeicoes'].append(1)

    if options['lunch']:
        payload['tiposRefeicoes'].append(2)

    if options['dinner']:
        payload['tiposRefeicoes'].append(3)

    response = http.post(
        f'{BASE_URL}/flutter/ru/agendaRefeicoes',
        json=payload,
        headers={
            'X-UFSM-Device-ID': device_id,
            'X-UFSM-Access-Name': config['environment']['app'],
            'X-UFSM-Version': API_VERSION,
            'X-UFSM-Access-Token': token,
            'Authorization': f'Bearer {token}'
        },
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()

    return unwrap_response(response)

def find_schedules(date):
    filtered_schedules = filter(
        lambda s : is_weekday(date, s['weekday']),
        config['schedules']
    )

    return list(filtered_schedules)

def use_monday_schedule_instead_of_saturday(current_date: datetime) -> bool:
    if not config['behavior'].get(FRIDAY_MONDAY_FALLBACK_KEY, False):
        return False

    if current_date.weekday() != 4:
        return False

    has_saturday_schedule = any(schedule.get('weekday') == 'Sat' for schedule in config['schedules'])
    has_sunday_schedule = any(schedule.get('weekday') == 'Sun' for schedule in config['schedules'])
    return not has_saturday_schedule and not has_sunday_schedule

def resolve_target_date(current_date: datetime) -> tuple[datetime, str]:
    if use_monday_schedule_instead_of_saturday(current_date):
        return current_date + timedelta(days=3), 'segunda-feira'
    return current_date + timedelta(days=1), 'amanhã'

parser = ArgumentParser(
    prog='ruina',
    description='Agenda automaticamente as refeições do RU da UFSM.'
)

parser.add_argument('-u', '--username', dest='username', default=os.getenv('RUINA_USERNAME'), help='Sua matrícula do aplicativo da UFSM.')
parser.add_argument('-p', '--password', dest='password', default=os.getenv('RUINA_PASSWORD'), help='Sua senha (prefira o prompt seguro ou RUINA_PASSWORD).')

args = parser.parse_args()

if not args.username:
    parser.error('informe sua matrícula com -u/--username')

if not args.password:
    args.password = getpass('Senha UFSM: ')

print('Lendo configuração...')
try:
    config = read_config()
except (OSError, ValueError, yaml.YAMLError) as exception:
    print(f'[Erro] Configuração inválida: {exception}')
    sys.exit(2)

print('Procurando refeições para serem agendadas...')
now = datetime.now(pytz.timezone('Brazil/East'))
target_date, target_label = resolve_target_date(now)

if target_label == 'segunda-feira':
    print('Sexta-feira detectada sem cronograma de sábado e domingo. Usando cronograma de segunda-feira.')

tomorrow_schedules = find_schedules(target_date)

if len(tomorrow_schedules) != 0:
    print(f'Encontrado {len(tomorrow_schedules)} refeição(s) para serem agendadas em {target_label}.')

    try:
        print('Logando no aplicativo...')
        access_token = login(args.username, args.password)
    except Exception as exception:
        print(f'Falha ao logar: {str(exception)}')
        sys.exit(1)
    else:
        failed = False

        for schedule_index, schedule in enumerate(tomorrow_schedules):
            if schedule_index > 0:
                print(f'Aguardando {SCHEDULE_COOLDOWN}s antes do próximo agendamento...')
                time.sleep(SCHEDULE_COOLDOWN)

            print(f"Agendando refeições para o RU {schedule['restaurant']}... ({schedule})")

            try:
                statuses = schedule_meal(access_token, target_date, target_date, schedule)
            except (requests.RequestException, RuntimeError, ValueError) as exception:
                print(f'[Erro] Falha ao agendar no RU {schedule["restaurant"]}: {exception}')
                failed = True
                continue

            if isinstance(statuses, dict):
                statuses = statuses.get('refeicoes', statuses.get('data', [statuses]))

            for status in statuses:
                if not isinstance(status, dict):
                    print(f'[Erro] RU {schedule["restaurant"]}: resposta inesperada da API: {status}')
                    failed = True
                    continue

                date_text = status.get('dataRefAgendada')
                date = datetime.strptime(date_text, '%Y-%m-%d %H:%M:%S') if date_text else target_date
                meal_type = status.get('tipoRefeicao', 'refeição')
                if isinstance(meal_type, dict):
                    meal_type = meal_type.get('descricao', 'refeição')
                message = (
                    f"{date.strftime('%d/%m/%Y')} - "
                    f"RU {schedule['restaurant']} ({meal_type}): "
                )

                if status.get('sucesso'):
                    print(message + 'Agendado com sucesso.')
                else:
                    reason = status.get('impedimento') or status.get('mensagem') or status.get('message')
                    if not reason:
                        reason = f'resposta da API sem sucesso: {status}'
                    print('[Erro] ' + message + reason.rstrip('.') + '.')
                    failed = True

        if failed:
            sys.exit(1)
else:
    print(f'Não há nenhuma refeição para ser agendada em {target_label}.')

