import logging
import logging.config
import os
import sys

class LowercaseFormatter(logging.Formatter):
    def format(self, record):
        record.levelname = record.levelname.lower()
        return super().format(record)

def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    ENVIRONMENT = os.getenv('ENVIRONMENT', 'local')
    LOGZIO_TOKEN = os.getenv('LOGZIO_TOKEN')
    
    if LOGZIO_TOKEN:
        LOGGING = {
            'version': 1,
            'disable_existing_loggers': False,
            'formatters': {
                'logzioFormat': {
                    '()': LowercaseFormatter,
                    'format': (
                        '{{"level": "%(levelname)s", "environment": "{environment}", "component": "openops mcp"}}'
                    ).format(environment=ENVIRONMENT),
                    'validate': False
                }
            },
            'handlers': {
                'logzio': {
                    'class': 'logzio.handler.LogzioHandler',
                    'level': 'INFO',
                    'formatter': 'logzioFormat',
                    'token': LOGZIO_TOKEN,
                    'logzio_type': 'openops-mcp',
                    'logs_drain_timeout': 5,
                    'url': 'https://listener.logz.io:8071',
                    'retries_no': 4,
                    'retry_timeout': 2,
                }
            },
            'loggers': {
                '': {
                    'level': 'DEBUG',
                    'handlers': ['logzio'],
                    'propagate': True
                }
            }
        }
        logging.config.dictConfig(LOGGING)
        logger.info("Logz.io logging configured successfully")

    return logger