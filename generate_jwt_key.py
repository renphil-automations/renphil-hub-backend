import secrets
import string

def generate_random_string(length=64):
    characters = string.ascii_letters + string.digits
    return ''.join(secrets.choice(characters) for _ in range(length))

if __name__ == "__main__":
    random_string = generate_random_string()
    print(random_string)