# Use the base image
FROM zyddnys/manga-image-translator:main

# Set the working directory
WORKDIR /app

# Copy the server script and other necessary files (if needed)
# COPY ./server ./server

# Set the entrypoint and command
ENTRYPOINT ["python"]
CMD ["server/main.py", "--verbose", "--start-instance", "--host=0.0.0.0", "--port=5003"]

# Expose the port
EXPOSE 5003

# Set up volume (optional, for runtime use)
# VOLUME ["/app/result"]