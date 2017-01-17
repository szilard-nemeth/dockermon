FROM python:2.7.13-slim

RUN pip install pyyaml

#RUN apt-get update && \
#    apt-get -y install python-yaml && \
#    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN apt-get update && \
    apt-get install -y gettext-base


ADD interpolate-env-vars.sh /
RUN chmod +x /*.sh
ADD dockermon.py /
ADD config.yml /
ADD logging.yaml /

CMD [ "python", "dockermon.py", "--config-file", "/config.yml" ]