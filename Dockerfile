FROM python:2.7.13-slim

ADD dockermon.py /

CMD [ "python", "dockermon.py" ]