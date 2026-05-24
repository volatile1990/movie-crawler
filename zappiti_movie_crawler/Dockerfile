ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk add --no-cache ffmpeg tzdata

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app
COPY run.sh /run.sh

RUN chmod a+x /run.sh

CMD ["/run.sh"]
